"""
Per-tenant rel_type overlay resolution (closes the per-tenant ontology loop).

ARCHITECTURE (authoritative — see CLAUDE.md "The Strengthening Layer",
"Schema Provisioning", and DEV/SELF-GROWTH-RESTORATION-PROMPT.md):

FaultLine is per-tenant. `public.rel_types` is a SHARED SEED ONLY. Each tenant's
grown/curated rel_types live in their own schema `faultline_<slug>.rel_types`.
Growth NEVER writes to public.

The runtime "known rel_type" reads (ingest classification `_REL_TYPE_META` and the
WGM gate `RelTypeRegistry`) historically opened fresh connections with no
search_path, so they resolved `public.rel_types` ONLY. A rel_type approved into a
tenant's own schema was therefore still invisible at runtime — the self-growth loop
never closed and curated per-tenant ontologies were dead at runtime.

This module resolves known rel_types as:

        public.rel_types (seed)  ∪  <tenant schema>.rel_types (grown/curated)

with the tenant's rows EXTENDING/OVERRIDING the seed. Isolation is preserved: a
request only ever reads its own tenant schema; growth never touches public.

HOT-PATH COST:
  • Seed (public) is cached globally with a TTL — one process-wide copy.
  • The per-tenant delta is cached per schema. A cache hit is DB-FREE (dict copy +
    merge of two in-memory dicts). The overlay is built lazily on first miss for a
    schema and on TTL expiry, and invalidated explicitly when that tenant approves a
    rel_type (via /internal/refresh-intent-pattern-caches?schema=...).
  • There is NO per-request full table scan on a warm cache.

THREAD/ASYNC SAFETY:
  • A single module-level RLock guards all cache mutation. Cache reads return copies
    so callers cannot mutate shared state.
  • The "current request schema" is carried in a ContextVar so the existing
    schema-blind global readers (e.g. classify_fact_3d reading _REL_TYPE_META)
    resolve the correct tenant overlay without threading a schema arg through ~20
    call sites. ContextVars are isolated per asyncio task and survive across awaits
    within the same task; the backend runs a single uvicorn worker.
"""

import os
import time
import threading
import contextvars

import psycopg2
import structlog

log = structlog.get_logger()

# Columns selected for the FULL meta dict (superset used by _build_rel_type_meta in
# main.py). The gate's RelTypeRegistry consumes a subset of these same keys, so one
# query/shape serves both readers.
_SELECT_COLS = (
    "rel_type, category, tail_types, head_types, storage_target, fact_class, "
    "is_symmetric, inverse_rel_type, is_hierarchy_rel, correction_behavior"
)

# TTL for both the global seed cache and per-schema overlays. Matches the prior
# RelTypeRegistry 5s live-refresh contract so behaviour is unchanged on the happy
# path; explicit invalidation closes the loop faster than the TTL.
_TTL_SECONDS = 5.0

_lock = threading.RLock()

# Global public seed cache: {"meta": {rel_type: {...}}, "loaded_at": float}
_seed_cache: dict = {"meta": {}, "loaded_at": 0.0}

# Per-schema overlay cache: {schema_name: {"meta": {rel_type: {...}}, "loaded_at": float}}
# "meta" here is the MERGED (seed ∪ tenant) view, ready to hand out.
_overlay_cache: dict[str, dict] = {}

# Current request's tenant schema. Schema-blind global readers consult this so they
# resolve the right tenant overlay. None / "public" → seed only.
_current_schema: contextvars.ContextVar = contextvars.ContextVar(
    "faultline_current_rel_type_schema", default=None
)


def set_current_schema(schema_name: str | None):
    """Bind the current request's tenant schema for schema-blind overlay reads.

    Returns the ContextVar token so the caller can reset() it after the request.
    Safe to call with None/"public" to mean seed-only.
    """
    return _current_schema.set(schema_name or None)


def reset_current_schema(token) -> None:
    """Restore the previous schema binding (best-effort)."""
    try:
        _current_schema.reset(token)
    except Exception:
        pass


def get_current_schema() -> str | None:
    return _current_schema.get()


def _row_to_meta(row) -> dict:
    (rel_type, category, tail_types, head_types, storage_target, fact_class,
     is_symmetric, inverse_rel_type, is_hierarchy_rel, correction_behavior) = row
    return {
        "category": category,
        "tail_types": tail_types or [],
        "head_types": head_types or [],
        "storage_target": storage_target,
        "fact_class": fact_class,
        "is_symmetric": is_symmetric or False,
        "inverse_rel_type": inverse_rel_type,
        "is_hierarchy_rel": is_hierarchy_rel or False,
        "correction_behavior": correction_behavior or "supersede",
    }


def _fetch_meta(dsn: str, schema_qualifier: str) -> dict:
    """Read rel_types from a single explicit schema. schema_qualifier is a bare,
    already-validated schema identifier (e.g. 'public' or 'faultline_<slug>').
    Returns {rel_type: meta_dict}.
    """
    meta: dict = {}
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_SELECT_COLS} FROM {schema_qualifier}.rel_types")
            for row in cur.fetchall():
                meta[row[0]] = _row_to_meta(row)
    return meta


def _get_seed(dsn: str) -> dict:
    """Return the cached public.rel_types seed meta (TTL-refreshed). Caller holds no
    lock; this acquires the lock internally. Returns a reference to the cached dict —
    callers must NOT mutate it (resolve_meta copies before merging)."""
    now = time.time()
    with _lock:
        if _seed_cache["meta"] and (now - _seed_cache["loaded_at"]) <= _TTL_SECONDS:
            return _seed_cache["meta"]
    # Build outside the lock (DB call), then commit under lock.
    try:
        fresh = _fetch_meta(dsn, "public")
    except Exception as e:
        log.warning("rel_type_overlay.seed_fetch_failed", error=str(e)[:160])
        with _lock:
            return dict(_seed_cache["meta"])  # last-known-good (possibly empty)
    with _lock:
        _seed_cache["meta"] = fresh
        _seed_cache["loaded_at"] = time.time()
        return _seed_cache["meta"]


def _is_real_tenant_schema(schema_name: str | None) -> bool:
    if not schema_name:
        return False
    s = schema_name.strip().lower()
    return s not in ("", "public")


def resolve_meta(dsn: str, schema_name: str | None) -> dict:
    """Resolve known rel_types as public seed ∪ tenant schema (tenant overrides).

    Returns a FRESH dict (safe for the caller to keep/mutate). Cache hit path performs
    no DB query for the seed (within TTL) and no DB query for the tenant overlay
    (within TTL) — only two in-memory dict merges.

    schema_name None / "public" → seed only (no tenant read; isolation trivially held).
    """
    if not dsn:
        return {}

    seed = _get_seed(dsn)

    if not _is_real_tenant_schema(schema_name):
        return dict(seed)

    schema_name = schema_name.strip()
    now = time.time()

    # Fast path: warm per-schema overlay.
    with _lock:
        entry = _overlay_cache.get(schema_name)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return dict(entry["meta"])

    # Miss / stale: fetch the tenant delta and merge over a fresh seed copy.
    try:
        tenant_meta = _fetch_meta(dsn, schema_name)
    except Exception as e:
        # Tenant schema unreadable (not provisioned, transient error): fall back to
        # seed-only. Never leak another tenant's data; never crash the hot path.
        log.warning("rel_type_overlay.tenant_fetch_failed",
                    schema=schema_name, error=str(e)[:160])
        return dict(seed)

    merged = dict(seed)
    merged.update(tenant_meta)  # tenant rows extend/override the seed

    with _lock:
        _overlay_cache[schema_name] = {"meta": merged, "loaded_at": time.time()}
    return dict(merged)


def resolve_current(dsn: str) -> dict:
    """Resolve meta for the ContextVar-bound current request schema (seed ∪ tenant)."""
    return resolve_meta(dsn, get_current_schema())


def invalidate(schema_name: str | None = None) -> None:
    """Invalidate caches.

    schema_name given  → drop that tenant's overlay (next read rebuilds it). This is
                          what the refresh endpoint calls when a tenant approves a
                          rel_type — only that tenant's overlay is rebuilt.
    schema_name None   → drop ALL overlays AND the seed (full reset; used for the
                          backward-compatible "refresh everything" path).
    """
    with _lock:
        if _is_real_tenant_schema(schema_name):
            _overlay_cache.pop(schema_name.strip(), None)
        else:
            _overlay_cache.clear()
            _seed_cache["meta"] = {}
            _seed_cache["loaded_at"] = 0.0
