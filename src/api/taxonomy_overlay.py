"""Per-tenant entity_taxonomies resolution (Phase 2 / 2C).

ARCHITECTURE (see DESIGN-query-scope-resolution.md Phase 2, CLAUDE.md
"Schema Provisioning", and the sibling module `rel_type_overlay.py`):

FaultLine is per-tenant. `public.entity_taxonomies` is a TEMPLATE / SEED-SOURCE
ONLY, read solely by provisioning. The seeder copies public → tenant at
provisioning time, so each tenant's own schema carries the full template PLUS its
grown/curated taxonomies and the user's own scope corrections — "my pets aren't
family". Growth and corrections NEVER write to public, and NO runtime read touches
public.

THE GAP THIS CLOSES:
The scope decisions in the query path historically consulted a single GLOBAL
module-level cache (`main._TAXONOMY_CACHE`) loaded ONCE at boot from `public`.
That made every per-tenant taxonomy customization invisible to scope resolution —
a user who edits "his definition of family" would not see it honoured, and a
future tenant-specific taxonomy could leak the wrong scope across tenants.

This module resolves taxonomy rows from the BOUND TENANT SCHEMA ONLY:

        <tenant>.entity_taxonomies   (seed-copied-at-provisioning ∪ curated)

Because provisioning seeds the full template into the tenant schema, no runtime
union with public is needed (or permitted). Isolation is preserved: a request
only ever reads its own tenant schema; a correction never touches public or
another tenant. The single read of `public.entity_taxonomies` is the
explicitly-unscoped fallback (no tenant bound: boot / anonymous), where there is
no tenant schema to read and public is the template of last resort.

It deliberately MIRRORS `rel_type_overlay.py` and REUSES the SAME request-schema
ContextVar (`rel_type_overlay._current_schema`, via `set_current_schema`) so a
single per-request binding governs BOTH resolvers. The only module-level state
here is the unscoped-fallback cache and the per-tenant cache, exactly as in the
rel_type module.

HOT-PATH COST: identical contract to rel_type_overlay — unscoped fallback cached
with a TTL, per-tenant data cached per schema, a warm hit is DB-free, explicit
invalidation on tenant edits.
"""

import time
import threading

import psycopg2
import structlog

# Reuse the SAME request-schema ContextVar binding as the rel_type overlay so one
# set_current_schema()/reset_current_schema() per request governs BOTH overlays.
from src.api import rel_type_overlay

log = structlog.get_logger()

# Columns selected for the taxonomy meta dict. `source` distinguishes
# user-edited/curated rows from seeded rows (Phase 3 scope corrections rely on
# it). It is read defensively: a tenant schema that predates the DDL alignment
# migration (no `source` column) still resolves — see _fetch_taxonomies.
_BASE_COLS = (
    "taxonomy_name, member_entity_types, rel_types_defining_group, description, "
    "has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type"
)

# TTL for both the global seed cache and per-schema overlays. Matches the
# rel_type overlay contract so behaviour is unchanged on the happy path; explicit
# invalidation closes the loop faster than the TTL.
_TTL_SECONDS = 5.0

_lock = threading.RLock()

# Unscoped-fallback cache (public template) — used ONLY when no tenant schema is
# bound (boot / anonymous). NEVER consulted on a real tenant binding.
# {"meta": {taxonomy_name: {...}}, "loaded_at": float}
_seed_cache: dict = {"meta": {}, "loaded_at": 0.0}

# Per-schema cache: {schema_name: {"meta": {...}, "loaded_at": float}}.
# "meta" is the TENANT-ONLY view (seed-copied-at-provisioning ∪ curated).
_overlay_cache: dict[str, dict] = {}

# Per-schema invalidation GENERATION — closes the same LOST-INVALIDATION race fixed in
# rel_type_overlay (a concurrent in-flight stale read repopulating the cache AFTER invalidate(),
# resurrecting pre-write taxonomy state mid-ingest). Reader snapshots the generation before its
# DB fetch and caches its snapshot ONLY IF the generation is unchanged. {schema_name: int}.
_overlay_generation: dict[str, int] = {}


def _row_to_meta(row) -> dict:
    (taxonomy_name, member_entity_types, rel_types_defining_group, description,
     has_transitivity, transitive_rel_types, is_hierarchical, parent_rel_type,
     source) = row
    return {
        "taxonomy_name": taxonomy_name,
        "member_entity_types": list(member_entity_types) if member_entity_types else [],
        "rel_types_defining_group": list(rel_types_defining_group) if rel_types_defining_group else [],
        "description": description,
        "has_transitivity": bool(has_transitivity),
        "transitive_rel_types": list(transitive_rel_types) if transitive_rel_types else [],
        "is_hierarchical": bool(is_hierarchical),
        "parent_rel_type": parent_rel_type,
        "source": source or "seeded",
    }


def _fetch_taxonomies(dsn: str, schema_qualifier: str) -> dict:
    """Read entity_taxonomies from a single explicit schema. `schema_qualifier`
    is a bare, already-validated schema identifier (e.g. 'public' or
    'faultline_<slug>'). Returns {taxonomy_name: meta_dict}.

    The `source` column may be absent in a tenant schema provisioned before the
    DDL-alignment migration. We probe for it and degrade gracefully (treat as
    'seeded') rather than fail the hot path — fail-loud is reserved for the
    seed/public read which must have the column.
    """
    meta: dict = {}
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'entity_taxonomies' "
                "AND column_name = 'source')",
                (schema_qualifier,),
            )
            has_source = cur.fetchone()[0]
            source_expr = "source" if has_source else "'seeded'::text AS source"
            cur.execute(
                f"SELECT {_BASE_COLS}, {source_expr} "
                f"FROM {schema_qualifier}.entity_taxonomies"
            )
            for row in cur.fetchall():
                meta[row[0]] = _row_to_meta(row)
    return meta


def _get_seed(dsn: str) -> dict:
    """Return the cached public.entity_taxonomies TEMPLATE meta (TTL-refreshed) for
    the UNSCOPED fallback path ONLY (no tenant bound). Callers must NOT mutate the
    returned dict (resolve_meta copies before returning).
    """
    now = time.time()
    with _lock:
        if _seed_cache["meta"] and (now - _seed_cache["loaded_at"]) <= _TTL_SECONDS:
            return _seed_cache["meta"]
    try:
        fresh = _fetch_taxonomies(dsn, "public")
    except Exception as e:
        log.warning("taxonomy_overlay.seed_fetch_failed", error=str(e)[:160])
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
    """Resolve taxonomies from the BOUND TENANT SCHEMA ONLY.

    Returns a FRESH dict {taxonomy_name: meta_dict} (safe to keep/mutate). Cache
    hit path performs no DB query (within TTL) — a dict copy.

    schema_name None / "public" → unscoped fallback: read the public template (the
        ONLY place public is read, and only because there is no tenant to read).
    schema_name = real tenant → read `<schema>.entity_taxonomies` ONLY (seed-copied
        at provisioning ∪ curated). If the tenant schema is unreadable we FAIL LOUD
        and return empty — we do NOT silently fall back to reading public (that would
        mask a provisioning/isolation failure and risk wrong scope).
    """
    if not dsn:
        return {}

    if not _is_real_tenant_schema(schema_name):
        # Genuinely tenant-less caller (boot / anonymous): public template fallback.
        return dict(_get_seed(dsn))

    schema_name = schema_name.strip()
    now = time.time()

    with _lock:
        entry = _overlay_cache.get(schema_name)
        if entry and (now - entry["loaded_at"]) <= _TTL_SECONDS:
            return dict(entry["meta"])
        gen_at_start = _overlay_generation.get(schema_name, 0)

    try:
        tenant_meta = _fetch_taxonomies(dsn, schema_name)
    except Exception as e:
        # Tenant schema unreadable (not provisioned / transient). FAIL LOUD — do NOT
        # read public for a bound tenant (that would mask the failure and risk wrong
        # scope). Return empty; the caller's own fail-safe applies.
        log.critical("taxonomy_overlay.tenant_fetch_failed",
                     schema=schema_name, error=str(e)[:160])
        return {}

    # Commit ONLY IF no invalidation/write landed during the fetch window (generation
    # unchanged) — otherwise return the fresh read but leave the cache empty so a newer write
    # always wins (lost-invalidation race fix; mirrors rel_type_overlay).
    with _lock:
        if _overlay_generation.get(schema_name, 0) == gen_at_start:
            _overlay_cache[schema_name] = {"meta": dict(tenant_meta), "loaded_at": time.time()}
        else:
            _overlay_cache.pop(schema_name, None)
    return dict(tenant_meta)


def resolve_current(dsn: str) -> dict:
    """Resolve taxonomies for the ContextVar-bound current request schema
    (tenant-only). Uses the SAME binding as the rel_type resolver."""
    return resolve_meta(dsn, rel_type_overlay.get_current_schema())


def invalidate(schema_name: str | None = None) -> None:
    """Invalidate caches.

    schema_name given  → drop that tenant's cache (next read rebuilds it). This
                          is what a scope correction calls so only that tenant's
                          cache is rebuilt.
    schema_name None   → drop ALL per-tenant caches AND the unscoped public-template
                          fallback cache (full reset).
    """
    with _lock:
        if _is_real_tenant_schema(schema_name):
            s = schema_name.strip()
            _overlay_cache.pop(s, None)
            _overlay_generation[s] = _overlay_generation.get(s, 0) + 1
        else:
            _overlay_cache.clear()
            _seed_cache["meta"] = {}
            _seed_cache["loaded_at"] = 0.0
            for _s in list(_overlay_generation.keys()):
                _overlay_generation[_s] = _overlay_generation.get(_s, 0) + 1
