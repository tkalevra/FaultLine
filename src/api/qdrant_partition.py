"""Qdrant partition choke-point — the ONE place that resolves a Qdrant op's target.

Ratified in ``DEV/DESIGN-qdrant-multitenancy-security.md``. This module is the vector-side
analogue of the Postgres ``subject_cursor`` factory: EVERY Qdrant read/search/scroll/delete/
upsert resolves its (collection, tenant-filter) here, and — in the consolidated model — an op
that would reach Qdrant WITHOUT a bound tenant filter THROWS rather than run a full-collection
scan (the poison-test guarantee).

Two modes, selected by env ``QDRANT_PARTITION_MODE``:

  • ``collection_per_seat`` (DEFAULT) — TODAY's exact behavior, BYTE-FOR-BYTE. The resolved
    collection name is the current ``derive_collection`` output and NO
    tenant filter is injected (``tenant_filter is None``). ``stamp_tenant`` is a no-op. This
    lets the code deploy changing nothing until the flag is flipped + data migrated.

  • ``shared_payload`` — the consolidated model. One shared collection
    ``faultline-memory``. Every point carries ``payload.tenant_id = <seat_uuid>``; every
    read/delete carries the ALWAYS-FILTER ``{must:[{key:tenant_id, match:{value:seat}}]}``.
    An unresolvable seat → ``tenant_filter is None`` → ``require_tenant`` THROWS.

Seams left for later phases (do NOT build here):
  • P2 (per-seat JWT): ``qdrant_headers()`` is where a minted, seat-scoped JWT bearer will be
    attached in place of / alongside the static service key — see ``_jwt_seam`` note.
  • P3 (clinical dedicated + encryption): ``resolve_partition`` is where an
    ``account_kind='clinical'`` seat will branch to a dedicated collection + per-seat payload
    encryption — see ``_clinical_seam`` note in ``resolve_partition``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Optional

# ── constants ────────────────────────────────────────────────────────────────────────────
MODE_COLLECTION_PER_SEAT = "collection_per_seat"
MODE_SHARED_PAYLOAD = "shared_payload"

SHARED_MEMORY_COLLECTION = "faultline-memory"

TENANT_KEY = "tenant_id"

KIND_MEMORY = "memory"

# Namespace for tenant-scoped point ids in shared mode. In ``collection_per_seat`` the point
# id is UUIDv5 over (source_table, fact_id) — collision-free WITHIN a seat's own collection.
# In the SHARED collection two seats' facts#5 would derive the SAME id and the second upsert
# would silently overwrite the first, so shared-mode ids MUST fold in the seat uuid.
_SHARED_POINT_NS = uuid.UUID("b7e6d3a2-1c4f-5e88-9a01-2f3e4d5c6b7a")

# Reserved values that cannot stand as a tenant in shared mode (no partitionable seat).
_UNRESOLVABLE = frozenset({"", "anonymous", "legacy", "test", "main"})


class UnboundTenantError(RuntimeError):
    """Raised when a shared-collection Qdrant op is attempted without a bound tenant filter.

    An unfiltered read/search/delete in the shared collection would scan/mutate EVERY tenant's
    points — a cross-tenant leak in the least-sacred (but still tenant) layer. Structural, not
    disciplinary: this is thrown, never logged-and-continued.
    """


# ── mode + auth ──────────────────────────────────────────────────────────────────────────
def partition_mode() -> str:
    """Resolve the active partition mode from env (default ``collection_per_seat``).

    Any unrecognized value falls back to the safe default (today's behavior), never to shared.
    """
    mode = (os.environ.get("QDRANT_PARTITION_MODE") or MODE_COLLECTION_PER_SEAT).strip().lower()
    return MODE_SHARED_PAYLOAD if mode == MODE_SHARED_PAYLOAD else MODE_COLLECTION_PER_SEAT


def shared_mode() -> bool:
    return partition_mode() == MODE_SHARED_PAYLOAD


def qdrant_headers() -> dict:
    """Headers every Qdrant HTTP call must send (P0 service-key auth).

    Returns ``{"api-key": <key>}`` when ``QDRANT_API_KEY`` is set, else ``{}`` (byte-for-byte
    unchanged when no key is configured — today's open-dev behavior).

    P2 SEAM (``_jwt_seam``): the per-seat JWT lands HERE — the backend will mint a short-lived
    token signed by the service key, carrying the ``tenant_id`` payload-filter claim, and this
    helper will return ``{"authorization": f"Bearer {jwt}"}`` derived from the bound seat uuid.
    Nothing about the call sites changes; only this resolver does.
    """
    key = os.environ.get("QDRANT_API_KEY")
    if key:
        return {"api-key": key}
    return {}


# ── tenant resolution ────────────────────────────────────────────────────────────────────
def tenant_value(user_id: Any) -> Optional[str]:
    """Normalize a user/seat id to its tenant partition value, or None if unpartitionable.

    A None result in shared mode means the op has no bound tenant → ``require_tenant`` throws.
    """
    if user_id is None:
        return None
    v = str(user_id).strip().lower()
    if v in _UNRESOLVABLE:
        return None
    return v


def tenant_must(user_id: Any) -> Optional[dict]:
    """The single ``must`` condition dict for a seat, or None if unresolvable."""
    v = tenant_value(user_id)
    if v is None:
        return None
    return {"key": TENANT_KEY, "match": {"value": v}}


def tenant_filter_for(user_id: Any) -> Optional[dict]:
    """Full ``{"must":[...]}`` tenant filter for a seat, or None if unresolvable/None."""
    cond = tenant_must(user_id)
    if cond is None:
        return None
    return {"must": [cond]}


def _derive_memory_collection(user_id: str) -> str:
    # Lazy import to avoid an import cycle (embedder imports this module).
    # Coerce a falsy id to "" so derive_collection returns the env default collection,
    # matching the legacy `derive_collection(user_id) if user_id else env` idiom exactly.
    from src.re_embedder.embedder import derive_collection
    return derive_collection(user_id or "")


def resolve_partition(user_id: Any, kind: str = KIND_MEMORY):
    """THE choke-point. Resolve ``(collection_name, tenant_filter)`` for a Qdrant op.

    collection_per_seat → (per-seat collection, None)  [today, byte-for-byte]
    shared_payload      → (shared collection, {"must":[{tenant_id: seat}]} or None)

    In shared mode a ``None`` tenant filter (unresolvable seat) is returned as-is; the caller
    passes it to ``require_tenant`` before touching Qdrant, which throws. Callers must NOT scan
    on a None filter.

    P3 SEAM (``_clinical_seam``): a clinical-tier seat will branch here to a dedicated
    collection (e.g. ``faultline-memory-clinical-<shard>``) + per-seat payload encryption,
    never the shared one. That branch keys off the account_kind resolved from ``user_id``.
    """
    if shared_mode():
        return SHARED_MEMORY_COLLECTION, tenant_filter_for(user_id)
    return _derive_memory_collection(user_id), None


def require_tenant(tenant_filter: Optional[dict], *, op: str = "qdrant", collection: str = "") -> None:
    """Structural guard: in shared mode a resolved op MUST carry a tenant filter.

    No-op in collection_per_seat mode (the per-seat collection IS the isolation boundary).
    In shared mode a ``None`` filter means the seat did not resolve to a partition → THROW,
    never run an unfiltered full-collection op. This is the poison-test guarantee.
    """
    if not shared_mode():
        return
    if not tenant_filter or not tenant_filter.get("must"):
        raise UnboundTenantError(
            f"refusing an unbound Qdrant {op} on shared collection {collection!r} — "
            "no tenant filter resolved (shared_payload mode requires a bound seat). "
            "An unfiltered shared-collection op is a cross-tenant leak."
        )


# ── payload stamping (writes) ────────────────────────────────────────────────────────────
def stamp_tenant(payload: Optional[dict], user_id: Any) -> dict:
    """Stamp ``tenant_id`` onto a point payload for a WRITE.

    No-op passthrough in collection_per_seat mode (byte-for-byte unchanged). In shared mode the
    bound seat value ALWAYS wins — a caller-supplied ``tenant_id`` cannot override it (a point
    can never be written into the wrong partition). Raises if the seat is unresolvable in shared
    mode (a shared write with no tenant is a bug, never a silent unpartitioned point).
    """
    out = dict(payload or {})
    if not shared_mode():
        return out
    v = tenant_value(user_id)
    if v is None:
        raise UnboundTenantError(
            "refusing to upsert a shared-collection Qdrant point with no resolvable tenant "
            f"(user_id={str(user_id)[:40]!r}) — every shared point must carry tenant_id."
        )
    out[TENANT_KEY] = v
    return out


# ── point-id derivation (collision-free in the shared collection) ─────────────────────────
def resolve_point_id(user_id: Any, source_table: str, fact_id: Any, kind: str = KIND_MEMORY) -> str:
    """Collision-free Qdrant point id for a fact/note.

    collection_per_seat → today's derivation (UUIDv5 over ``source_table:fact_id``).
    shared_payload → fold the seat uuid in so two seats' ``facts#5`` never collide into one
    shared point.
    """
    if not shared_mode():
        from src.re_embedder.embedder import derive_qdrant_point_id
        return derive_qdrant_point_id(source_table, fact_id)
    v = tenant_value(user_id)
    if v is None:
        raise UnboundTenantError("refusing to derive a shared point id with no resolvable tenant")
    return str(uuid.uuid5(_SHARED_POINT_NS, f"{v}:{source_table}:{int(fact_id)}"))


# ── filter/body composition (reads, searches, scrolls, deletes) ──────────────────────────
def merge_filter(existing: Optional[dict], tenant_filter: Optional[dict]) -> Optional[dict]:
    """AND a tenant filter into an existing Qdrant filter (or return ``existing`` unchanged
    when ``tenant_filter is None``, i.e. collection_per_seat).

    Never mutates the inputs. Preserves any ``should``/``must_not`` on ``existing``.
    """
    if not tenant_filter:
        return existing
    tenant_must_list = list(tenant_filter.get("must") or [])
    if not existing:
        return {"must": tenant_must_list}
    merged = dict(existing)
    merged["must"] = list(existing.get("must") or []) + tenant_must_list
    return merged


def apply_tenant_filter(body: dict, tenant_filter: Optional[dict]) -> dict:
    """Return a COPY of a search/scroll/query body with the tenant filter merged into
    ``body['filter']``. No-op (shallow copy) when ``tenant_filter is None``."""
    out = dict(body or {})
    merged = merge_filter(out.get("filter"), tenant_filter)
    if merged is not None:
        out["filter"] = merged
    return out


def build_delete_body(
    tenant_filter: Optional[dict],
    *,
    point_ids: Optional[list] = None,
    must: Optional[list] = None,
) -> dict:
    """Build a ``/points/delete`` body that is ALWAYS tenant-scoped in shared mode.

    collection_per_seat (tenant_filter None):
        point_ids → ``{"points": point_ids}``  (today's bare-id selector)
        must      → ``{"filter": {"must": must}}``

    shared_payload (tenant_filter set):
        point_ids → ``{"filter": {"must": [tenant, {"has_id": point_ids}]}}`` — a bare-id
            delete in the shared collection could hit a colliding id from ANOTHER tenant, so
            deletes are converted to a filter form AND-ed with the tenant predicate + has_id.
        must      → tenant AND-ed with the caller's ``must``.
    """
    if not tenant_filter:
        # collection_per_seat — today's shapes, byte-for-byte.
        if point_ids is not None:
            return {"points": list(point_ids)}
        return {"filter": {"must": list(must or [])}}
    tenant_must_list = list(tenant_filter.get("must") or [])
    extra = list(must or [])
    if point_ids is not None:
        extra = extra + [{"has_id": list(point_ids)}]
    return {"filter": {"must": tenant_must_list + extra}}


# ── shared-collection provisioning ───────────────────────────────────────────────────────
def ensure_tenant_index(collection: str, qdrant_url: str, *, timeout: float = 10.0) -> bool:
    """Create the ``tenant_id`` payload index with ``is_tenant=true`` (Qdrant co-locates each
    tenant's points on disk → fast filtered search). Idempotent: an already-present index is
    success. Fail-safe: returns False on failure, never raises."""
    import httpx
    try:
        r = httpx.put(
            f"{qdrant_url.rstrip('/')}/collections/{collection}/index",
            json={"field_name": TENANT_KEY, "field_schema": {"type": "keyword", "is_tenant": True}},
            headers=qdrant_headers(),
            timeout=timeout,
        )
        # 200 = created; Qdrant returns 200 on re-issue too. Treat 4xx "already exists" as ok.
        return r.status_code in (200, 409)
    except Exception:
        return False


def ensure_shared_collection(collection: str, qdrant_url: str, *, dim: int = 768,
                             timeout: float = 10.0) -> bool:
    """Idempotently create a shared collection (768-d cosine) + its ``tenant_id`` index.

    Only used in shared mode / by the migration. Fail-safe: False on failure, never raises."""
    import httpx
    base = qdrant_url.rstrip("/")
    try:
        r = httpx.get(f"{base}/collections/{collection}", headers=qdrant_headers(), timeout=timeout)
        if r.status_code == 404:
            c = httpx.put(
                f"{base}/collections/{collection}",
                json={"vectors": {"size": dim, "distance": "Cosine"}},
                headers=qdrant_headers(),
                timeout=timeout,
            )
            if c.status_code != 200:
                return False
        elif r.status_code != 200:
            return False
        # Always (re)assert the tenant index — idempotent.
        ensure_tenant_index(collection, base, timeout=timeout)
        return True
    except Exception:
        return False
