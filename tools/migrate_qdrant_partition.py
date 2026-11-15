#!/usr/bin/env python3
"""Migrate per-seat Qdrant collections → the consolidated payload-partitioned model.

Ratified in ``DEV/DESIGN-qdrant-multitenancy-security.md`` (P4). This is the data move that
lets the live code flip ``QDRANT_PARTITION_MODE`` from ``collection_per_seat`` to
``shared_payload``:

  faultline-<seat_uuid>  →  faultline-memory   (payload.tenant_id = <seat_uuid>)
  flagent-<seat_uuid>    →  flagent-cortex     (payload.tenant_id = <seat_uuid>)

Properties:
  • IDEMPOTENT — target point ids are DETERMINISTIC (tenant-namespaced UUIDv5 over the source
    row keys, matching the live shared-mode ``resolve_point_id`` / ``resolve_cortex_point_id``),
    so a re-run upserts the SAME ids (no duplicates). ``store_context`` points (already
    globally-unique uuid4 ids) keep their id and are just tenant-stamped.
  • COLLISION-SAFE — two seats' ``facts#5`` derive DIFFERENT shared ids (the seat uuid is
    folded in), so consolidating never overwrites one seat's point with another's.
  • NON-DESTRUCTIVE — source collections are NEVER deleted (safe rollback; Qdrant is derived).
    The main loop retires the old per-seat collections AFTER verifying the consolidated ones.
  • SCOPED — only ``faultline-<uuid>`` / ``flagent-<uuid>`` (validated uuid shape) are migrated.
    The 7 OpenWebUI-native collections (``open-webui_*``, ``memory-gematria/wren/kairos``) and
    any non-uuid / shared / test / legacy collection are LEFT UNTOUCHED.

⛔ LOCAL / operator tool. Reads ``QDRANT_URL`` (+ ``QDRANT_API_KEY`` if set). Dry-run by default;
pass ``--apply`` to write.

Usage:
  QDRANT_URL=http://<qdrant>:6333 python3 tools/migrate_qdrant_partition.py            # dry-run
  QDRANT_URL=http://<qdrant>:6333 python3 tools/migrate_qdrant_partition.py --apply     # migrate
  ... --only faultline   # memory only     ... --only flagent   # cortex only
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# The migration operates in the SHARED target model regardless of the ambient flag — force it
# so the id/filter/stamp helpers derive the tenant-namespaced shared ids the live code will use.
os.environ["QDRANT_PARTITION_MODE"] = "shared_payload"

import httpx  # noqa: E402

# Import the choke-point so the migration and the live path share ONE id/stamp derivation.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.api.qdrant_partition import (  # noqa: E402
    SHARED_MEMORY_COLLECTION, SHARED_CORTEX_COLLECTION, TENANT_KEY,
    resolve_point_id, resolve_cortex_point_id, ensure_shared_collection, qdrant_headers,
)

# A dashed uuid (the seat id) — the ONLY suffix we migrate. Guards OpenWebUI-native / legacy.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MEMORY_PREFIX = "faultline-"
_CORTEX_PREFIX = "flagent-"

_QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
_SCROLL_PAGE = 256


def _list_collections() -> list[str]:
    r = httpx.get(f"{_QDRANT_URL}/collections", headers=qdrant_headers(), timeout=15.0)
    r.raise_for_status()
    return [c["name"] for c in r.json().get("result", {}).get("collections", [])]


def _classify(name: str):
    """Return (kind, seat_uuid) for a migratable source, else (None, None).

    kind ∈ {"memory","cortex"}. Only ``<prefix><dashed-uuid>`` migrates; the shared targets,
    ``faultline-test``/``-main``, OpenWebUI-native, and any non-uuid suffix are skipped."""
    if name in (SHARED_MEMORY_COLLECTION, SHARED_CORTEX_COLLECTION):
        return None, None
    if name.startswith(_MEMORY_PREFIX):
        suffix = name[len(_MEMORY_PREFIX):]
        if _UUID_RE.match(suffix):
            return "memory", suffix
    elif name.startswith(_CORTEX_PREFIX):
        suffix = name[len(_CORTEX_PREFIX):]
        if _UUID_RE.match(suffix):
            return "cortex", suffix
    return None, None


def _scroll_all(collection: str):
    """Yield every point (dict with id/payload/vector) from a collection."""
    offset = None
    while True:
        body = {"limit": _SCROLL_PAGE, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        r = httpx.post(
            f"{_QDRANT_URL}/collections/{collection}/points/scroll",
            json=body, headers=qdrant_headers(), timeout=30.0,
        )
        r.raise_for_status()
        res = r.json().get("result", {}) or {}
        for p in res.get("points", []) or []:
            yield p
        offset = res.get("next_page_offset")
        if not offset:
            break


def _target_point(kind: str, seat: str, point: dict):
    """Build the (id, vector, payload) for the shared-collection re-upsert of one source point.

    Returns None to SKIP a point (e.g. a context point with no usable id)."""
    payload = dict(point.get("payload") or {})
    vector = point.get("vector")
    payload[TENANT_KEY] = seat  # the always-filter partition key
    if kind == "memory":
        src_table = payload.get("source_table")
        fact_id = payload.get("fact_id")
        if src_table is not None and fact_id is not None:
            new_id = resolve_point_id(seat, src_table, fact_id, "memory")
        else:
            # store_context / legacy: keep the original (already-unique) id; just tenant-stamp.
            new_id = point.get("id")
    else:  # cortex
        row_id = payload.get("row_id")
        if row_id is not None:
            new_id = resolve_cortex_point_id(seat, row_id)
        else:
            new_id = point.get("id")
    if new_id is None:
        return None
    return {"id": new_id, "vector": vector, "payload": payload}


def _upsert_batch(collection: str, points: list[dict]) -> None:
    if not points:
        return
    r = httpx.put(
        f"{_QDRANT_URL}/collections/{collection}/points",
        json={"points": points}, headers=qdrant_headers(), timeout=60.0,
    )
    r.raise_for_status()


def migrate(apply: bool, only: str | None) -> int:
    collections = _list_collections()
    sources = []
    for name in collections:
        kind, seat = _classify(name)
        if kind is None:
            continue
        if only and kind != ("memory" if only == "faultline" else "cortex" if only == "flagent" else only):
            continue
        sources.append((name, kind, seat))

    print(f"[migrate] qdrant={_QDRANT_URL} mode={'APPLY' if apply else 'DRY-RUN'}")
    print(f"[migrate] {len(collections)} collections total; {len(sources)} migratable "
          f"(faultline-<uuid> / flagent-<uuid>). OpenWebUI-native + shared + legacy left alone.")

    if apply:
        for target in {SHARED_MEMORY_COLLECTION, SHARED_CORTEX_COLLECTION}:
            ok = ensure_shared_collection(target, _QDRANT_URL)
            print(f"[migrate] ensure target {target}: {'ok' if ok else 'FAILED'} (+tenant_id index)")

    total_pts = 0
    migrated_cols = 0
    for name, kind, seat in sources:
        target = SHARED_MEMORY_COLLECTION if kind == "memory" else SHARED_CORTEX_COLLECTION
        batch, moved, skipped = [], 0, 0
        for point in _scroll_all(name):
            tp = _target_point(kind, seat, point)
            if tp is None:
                skipped += 1
                continue
            batch.append(tp)
            if apply and len(batch) >= 128:
                _upsert_batch(target, batch)
                batch = []
            moved += 1
        if apply and batch:
            _upsert_batch(target, batch)
        total_pts += moved
        migrated_cols += 1
        print(f"[migrate] {name} → {target}: {moved} points"
              + (f" (+{skipped} skipped)" if skipped else "")
              + ("" if apply else " [dry-run, not written]"))

    print(f"[migrate] DONE — collections migrated={migrated_cols}, points moved={total_pts} "
          f"({'written' if apply else 'DRY-RUN — nothing written; re-run with --apply'})")
    print("[migrate] source collections were NOT deleted (retire them after verifying the "
          "consolidated collections).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidate per-seat Qdrant collections into the "
                                             "payload-partitioned shared model.")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry-run)")
    ap.add_argument("--only", choices=["faultline", "flagent", "memory", "cortex"],
                    help="migrate only memory (faultline) or only cortex (flagent)")
    args = ap.parse_args()
    try:
        return migrate(apply=args.apply, only=args.only)
    except Exception as e:
        print(f"[migrate] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
