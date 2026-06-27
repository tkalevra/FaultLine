"""
Per-tenant rel_type resolution (closes the per-tenant ontology loop).

ARCHITECTURE (authoritative — see CLAUDE.md "The Strengthening Layer",
"Schema Provisioning", and DEV/SELF-GROWTH-RESTORATION-PROMPT.md):

FaultLine is per-tenant. `public.rel_types` is a TEMPLATE / SEED-SOURCE ONLY,
read solely by provisioning/migrations. Provisioning copies the seed INTO each
tenant schema (`faultline_<slug>.rel_types`), and each tenant's grown/curated
rel_types live in that same schema. Growth NEVER writes to public, and NO runtime
read touches public.

The runtime "known rel_type" reads (ingest classification `_REL_TYPE_META` and the
WGM gate `RelTypeRegistry`) historically opened fresh connections with no
search_path, so they resolved `public.rel_types` ONLY. A rel_type approved into a
tenant's own schema was therefore still invisible at runtime — the self-growth loop
never closed and curated per-tenant ontologies were dead at runtime.

This module resolves known rel_types from the BOUND TENANT SCHEMA ONLY:

        <tenant schema>.rel_types   (seed-copied-at-provisioning ∪ grown/curated)

Because provisioning seeds the full template into the tenant schema, the tenant
schema already carries every seed row PLUS the tenant's grown rows — no runtime
union with public is needed (or permitted). A request only ever reads its own
tenant schema; growth never touches public; there is no cross-tenant seam.

The only read of `public.rel_types` here is the explicitly-unscoped fallback
(schema_name None / "public") used by genuinely tenant-less callers (boot /
anonymous), where there is no tenant schema to read and public is the template of
last resort. A real tenant binding NEVER reads public.

HOT-PATH COST:
  • The per-tenant meta is cached per schema. A cache hit is DB-FREE (dict copy).
    It is built lazily on first miss for a schema and on TTL expiry, and
    invalidated explicitly when that tenant approves a rel_type (via
    /internal/refresh-intent-pattern-caches?schema=...).
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
    "is_symmetric, inverse_rel_type, is_hierarchy_rel, correction_behavior, "
    "label, natural_language, natural_language_2p, temporal_class, source, "
    # SCALAR-TYPE discipline (migration 101): the datatype of a SCALAR slot + its range/unit.
    # Drives metadata-driven per-datatype validation + datatype-aware coercion at ingest.
    "scalar_datatype, value_min, value_max, unit"
)

# TTL for both the unscoped-seed cache and per-tenant caches. Matches the prior
# RelTypeRegistry 5s live-refresh contract so behaviour is unchanged on the happy
# path; explicit invalidation closes the loop faster than the TTL.
_TTL_SECONDS = 5.0

_lock = threading.RLock()

# Unscoped-fallback cache (public template) — used ONLY when no tenant schema is
# bound (boot / anonymous). NEVER consulted on a real tenant binding.
# {"meta": {rel_type: {...}}, "loaded_at": float}
_seed_cache: dict = {"meta": {}, "loaded_at": 0.0}

# Per-schema cache: {schema_name: {"meta": {rel_type: {...}}, "loaded_at": float}}.
# "meta" is the TENANT-ONLY view (seed-copied-at-provisioning ∪ grown), ready to hand out.
_overlay_cache: dict[str, dict] = {}

# Per-schema invalidation GENERATION counter — closes a LOST-INVALIDATION race that made
# ingest non-deterministic. Without it, a concurrent in-flight STALE read (started before a
# write) could repopulate _overlay_cache[schema] AFTER invalidate() ran, resurrecting the
# pre-write snapshot. The WGM gate's in-flow constraint-EXPANSION (gate.py: infer rel_type
# metadata via LLM → UNION-update rel_types → invalidate → RE-CHECK constraints through this
# overlay) would then read the stale snapshot and REJECT the just-admitted edge ~1-in-6 — the
# exact "same input → a fact silently flickers" non-determinism. A reader snapshots the
# generation BEFORE its DB fetch and commits its result ONLY IF the generation is unchanged
# (no invalidation/write landed during the fetch window); otherwise it returns its fresh read
# but does NOT cache it, so a newer write always wins. Monotonic, lock-guarded, subject-
# agnostic. {schema_name: int}; missing → 0.
_overlay_generation: dict[str, int] = {}

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
     is_symmetric, inverse_rel_type, is_hierarchy_rel, correction_behavior,
     label, natural_language, natural_language_2p, temporal_class, source,
     scalar_datatype, value_min, value_max, unit) = row
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
        # Render templates (consumed by convert_to_prose via the per-tenant overlay so
        # grown/curated tenant rel_types render with a verb without reading public.*).
        "label": label,
        "natural_language": natural_language,
        "natural_language_2p": natural_language_2p,
        # Temporal class (migration 096): immutable | state | event. SAFE DEFAULT 'state'
        # — the non-destructive class — when a row's column is NULL/absent. Phase 0 only
        # RESOLVES it; Phase 1 reads it to gate event_date stamping (immutable → no date).
        "temporal_class": temporal_class or "state",
        # Provenance of the rel_type itself: 'wikidata'/'builtin' = the SEEDED canonical
        # backbone (never tenant-deletable/evictable from candidate sets); other values =
        # tenant-grown. Read by the candidate-set builders to reserve the backbone's slots.
        "source": source,
        # SCALAR-TYPE discipline (migration 101): the datatype of a SCALAR slot (closed
        # XSD/Wikidata-literal set), with optional range/unit. NULL for non-SCALAR rels.
        # Consumed by the ingest scalar validator registry + datatype-aware coercion.
        "scalar_datatype": scalar_datatype,
        "value_min": value_min,
        "value_max": value_max,
        "unit": unit,
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
    """Return the cached public.rel_types TEMPLATE meta (TTL-refreshed) for the
    UNSCOPED fallback path ONLY (no tenant bound). Caller holds no lock; this
    acquires the lock internally. Returns a reference to the cached dict — callers
    must NOT mutate it (resolve_meta copies before returning)."""
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
    """Resolve known rel_types from the BOUND TENANT SCHEMA ONLY.

    Returns a FRESH dict (safe for the caller to keep/mutate). Cache hit path performs
    no DB query (within TTL) — a dict copy.

    schema_name None / "public" → unscoped fallback: read the public template (the
        ONLY place public is read, and only because there is no tenant to read).
    schema_name = real tenant → read `<schema>.rel_types` ONLY. The tenant schema was
        seeded with the full template at provisioning and carries the grown rows on
        top, so no union with public is needed. If the tenant schema is unreadable we
        FAIL LOUD and return empty — we do NOT silently fall back to reading public
        (that would mask a provisioning/isolation failure).
    """
    if not dsn:
        return {}

    if not _is_real_tenant_schema(schema_name):
        # Genuinely tenant-less caller (boot / anonymous): public template fallback.
        return dict(_get_seed(dsn))

    schema_name = schema_name.strip()
    now = time.time()

    # Fast path: warm per-tenant cache. Capture the invalidation GENERATION under the same
    # lock as the cache read so a write that lands AFTER this point is detectable below.
    with _lock:
        entry = _overlay_cache.get(schema_name)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return dict(entry["meta"])
        gen_at_start = _overlay_generation.get(schema_name, 0)

    # Miss / stale: read the tenant schema only (seed-copied ∪ grown, no public union).
    try:
        tenant_meta = _fetch_meta(dsn, schema_name)
    except Exception as e:
        # Tenant schema unreadable (not provisioned / transient). FAIL LOUD — do NOT
        # read public for a bound tenant (that would mask the failure and risk wrong
        # scope). Return empty; the caller's own fail-safe applies.
        log.critical("rel_type_overlay.tenant_fetch_failed",
                     schema=schema_name, error=str(e)[:160])
        return {}

    # Commit ONLY IF no invalidation/write landed during the fetch window (generation
    # unchanged). If the generation moved, a concurrent writer (e.g. the WGM gate expanding a
    # type constraint) invalidated this schema while we were reading stale rows — caching our
    # snapshot now would resurrect the pre-write state (the lost-invalidation race that made
    # ingest flicker). In that case we still RETURN our fresh read (correct for THIS caller's
    # request, fail-safe), but leave the cache empty so the next read re-fetches the
    # post-write rows. Deterministic: a newer write always wins the cache.
    with _lock:
        if _overlay_generation.get(schema_name, 0) == gen_at_start:
            _overlay_cache[schema_name] = {"meta": dict(tenant_meta), "loaded_at": time.time()}
        else:
            _overlay_cache.pop(schema_name, None)
    return dict(tenant_meta)


def resolve_current(dsn: str) -> dict:
    """Resolve meta for the ContextVar-bound current request schema (tenant-only)."""
    return resolve_meta(dsn, get_current_schema())


def invalidate(schema_name: str | None = None) -> None:
    """Invalidate caches.

    schema_name given  → drop that tenant's cache (next read rebuilds it). This is
                          what the refresh endpoint calls when a tenant approves a
                          rel_type — only that tenant's cache is rebuilt.
    schema_name None   → drop ALL per-tenant caches AND the unscoped public-template
                          fallback cache (full reset; "refresh everything" path).
    """
    with _lock:
        if _is_real_tenant_schema(schema_name):
            s = schema_name.strip()
            _overlay_cache.pop(s, None)
            # Bump the generation so any concurrent in-flight read that started BEFORE this
            # invalidation cannot repopulate the cache with its now-stale snapshot (lost-
            # invalidation race → flaky ingest). Monotonic per schema.
            _overlay_generation[s] = _overlay_generation.get(s, 0) + 1
        else:
            _overlay_cache.clear()
            _seed_cache["meta"] = {}
            _seed_cache["loaded_at"] = 0.0
            # Full reset → bump EVERY known schema's generation (cancel all in-flight reads).
            for _s in list(_overlay_generation.keys()):
                _overlay_generation[_s] = _overlay_generation.get(_s, 0) + 1
