"""
FaultLine Re-Embedder Service

Polls the facts table for unsynced rows, embeds them, and upserts to per-user Qdrant collections.
This is the only service that writes to Qdrant.
"""
import hashlib
import json
import logging
import os
import time

import httpx
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger(__name__)


def derive_collection(user_id: str) -> str:
    """Derive Qdrant collection name from user_id."""
    if user_id in ("", "anonymous", "legacy"):
        return os.getenv("QDRANT_COLLECTION", "faultline-test")
    return f"faultline-{user_id}"


def fetch_unsynced(db_conn, confidence_threshold: float = 0.0) -> list[dict]:
    """Fetch all non-superseded facts where qdrant_synced = false and confidence >= threshold."""
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, subject_id, object_id, rel_type, provenance, user_id,
                   confidence, confirmed_count, last_seen_at, contradicted_by
            FROM facts
            WHERE qdrant_synced = false AND (superseded_at IS NULL)
            AND confidence >= %s
            ORDER BY id ASC
            """,
            (confidence_threshold,)
        )
        rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "subject_id": row[1],
            "object_id": row[2],
            "rel_type": row[3],
            "provenance": row[4],
            "user_id": row[5] if row[5] else "anonymous",
            "confidence": row[6] if row[6] is not None else 1.0,
            "confirmed_count": row[7] if row[7] is not None else 0,
            "last_seen_at": row[8],
            "contradicted_by": row[9],
        }
        for row in rows
    ]


def fetch_unsynced_staged(db_conn) -> list[dict]:
    """Fetch staged_facts where qdrant_synced = false and not yet promoted or expired."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject_id, object_id, rel_type, provenance, user_id,
                   confidence, confirmed_count, last_seen_at, fact_class
            FROM staged_facts
            WHERE qdrant_synced = false
              AND promoted_at IS NULL
              AND expires_at > now()
            ORDER BY id ASC
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "subject_id": row[1],
            "object_id": row[2],
            "rel_type": row[3],
            "provenance": row[4],
            "user_id": row[5] or "anonymous",
            "confidence": row[6] if row[6] is not None else 0.6,
            "confirmed_count": row[7] if row[7] is not None else 0,
            "last_seen_at": row[8],
            "contradicted_by": None,
            "staged_id": row[0],
            "fact_class": row[9],
        }
        for row in rows
    ]


def resolve_display_names_for_facts(db_conn, rows: list[dict]) -> list[dict]:
    """
    For each fact row, resolve subject_id and object_id UUID surrogates
    to their preferred display names via entity_aliases.
    Falls back to the UUID string if no alias is found.
    Returns a new list of dicts with added 'subject_display' and 'object_display' keys.
    """
    if not rows:
        return rows

    # Collect all unique entity IDs across all rows
    entity_ids = set()
    for row in rows:
        entity_ids.add(row["subject_id"])
        entity_ids.add(row["object_id"])

    # Batch lookup preferred aliases
    display_map = {}
    try:
        placeholders = ",".join(["%s"] * len(entity_ids))
        with db_conn.cursor() as cur:
            cur.execute(
                f"SELECT entity_id, alias FROM entity_aliases "
                f"WHERE entity_id IN ({placeholders}) AND is_preferred = true",
                list(entity_ids),
            )
            for entity_id, alias in cur.fetchall():
                display_map[entity_id] = alias
    except Exception as e:
        log.warning(f"re_embedder.display_name_lookup_failed: {e}")

    # Attach display names to each row
    resolved = []
    for row in rows:
        resolved.append({
            **row,
            "subject_display": display_map.get(row["subject_id"], row["subject_id"]),
            "object_display": display_map.get(row["object_id"], row["object_id"]),
        })
    return resolved


def embed_text(text: str, qwen_api_url: str, timeout: float = 30.0, fallback: bool = True) -> list[float] | None:
    """
    Embed text using the nomic-embed-text model via the Ollama/Qwen API.

    fallback=True  (default, used by re_embedder): returns a hash vector on failure so
                   the re_embedder loop keeps running.
    fallback=False (used by /query):               returns None on failure so the caller
                   can skip the Qdrant search rather than searching with a meaningless vector.
    """
    embed_url = qwen_api_url.replace("/chat/completions", "/embeddings")

    try:
        response = httpx.post(
            embed_url,
            json={"model": "text-embedding-nomic-embed-text-v1.5", "input": text},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        if "data" in data and len(data["data"]) > 0:
            return data["data"][0]["embedding"]

        raise ValueError("Invalid embedding response format")

    except Exception as e:
        if fallback:
            log.warning(f"re_embedder.embed_failed text_preview={text[:50]} falling back to hash vector: {e}")
            return hash_vector(text)
        log.error(f"re_embedder.embed_failed text_preview={text[:50]} no fallback: {e}")
        return None


def hash_vector(text: str, size: int = 768) -> list[float]:
    """
    Generate deterministic hash-based vector from text.
    Same text always produces same vector.
    """
    # Use SHA256 hash of text as seed
    hash_bytes = hashlib.sha256(text.encode('utf-8')).digest()

    # Convert to deterministic float values in range [-1, 1]
    vector = []
    for i in range(size):
        # Use modulo to cycle through hash bytes
        byte_val = hash_bytes[i % len(hash_bytes)]
        # Normalize to [-1, 1]
        normalized = (byte_val / 255.0) * 2.0 - 1.0
        vector.append(normalized)

    return vector


def ensure_collection(collection: str, qdrant_url: str) -> bool:
    """
    Check if Qdrant collection exists, create if not.
    Returns True if collection exists or was created, False on failure.
    """
    try:
        response = httpx.get(
            f"{qdrant_url}/collections/{collection}",
            timeout=10.0
        )

        if response.status_code == 200:
            return True

        if response.status_code == 404:
            # Create collection
            create_response = httpx.put(
                f"{qdrant_url}/collections/{collection}",
                json={
                    "vectors": {
                        "size": 768,
                        "distance": "Cosine"
                    }
                },
                timeout=10.0
            )

            if create_response.status_code == 200:
                log.info(f"re_embedder.collection_created collection={collection}")
                return True
            else:
                log.error(f"re_embedder.collection_create_failed collection={collection} status={create_response.status_code}")
                return False

        log.error(f"re_embedder.collection_check_unexpected collection={collection} status={response.status_code}")
        return False

    except Exception as e:
        log.error(f"re_embedder.collection_check_failed collection={collection} error={e}")
        return False


def upsert_to_qdrant(row: dict, vector: list[float], collection: str, qdrant_url: str) -> bool:
    """
    Upsert fact embedding to Qdrant collection.
    Returns True on success, False on failure.
    """
    try:
        response = httpx.put(
            f"{qdrant_url}/collections/{collection}/points",
            json={
                "points": [
                    {
                        "id": int(row["id"]),
                        "vector": vector,
                        "payload": {
                            "subject": row.get("subject_display", row["subject_id"]),
                            "object": row.get("object_display", row["object_id"]),
                            "rel_type": row["rel_type"],
                            "provenance": row["provenance"],
                            "user_id": row["user_id"],
                            "fact_id": int(row["id"]),
                            "confidence": row.get("confidence", 1.0),
                            "confirmed_count": row.get("confirmed_count", 0),
                            "last_seen_at": row["last_seen_at"].isoformat() if row.get("last_seen_at") else None,
                            "contradicted": row.get("contradicted_by") is not None,
                        }
                    }
                ]
            },
            timeout=30.0
        )

        if response.status_code == 200:
            return True

        log.error(f"re_embedder.qdrant_error fact_id={row['id']} status={response.status_code} body={response.text}")
        return False

    except Exception as e:
        log.error(f"re_embedder.qdrant_error fact_id={row['id']}: {e}")
        return False


def mark_synced(db_conn, fact_id: int) -> None:
    """Mark a fact as synced to Qdrant."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE facts SET qdrant_synced = true WHERE id = %s",
            (fact_id,)
        )
    db_conn.commit()


def promote_facts(db_conn) -> None:
    """Promote facts to long-term memory by increasing confidence for eligible facts."""
    with db_conn.cursor() as cur:
        # Increase confidence for facts that have been confirmed multiple times or are old
        cur.execute(
            """
            UPDATE facts
            SET confidence = LEAST(confidence + 0.1, 1.0)
            WHERE superseded_at IS NULL
            AND (confirmed_count >= 2 OR last_seen_at < now() - interval '7 days')
            AND confidence < 1.0
            """
        )
        promoted_count = cur.rowcount
        if promoted_count > 0:
            log.info(f"re_embedder.promoted {promoted_count} facts to long-term memory")
    db_conn.commit()


def promote_staged_facts(db_conn, qdrant_url: str, promotion_threshold: int = 3) -> int:
    """
    Promote Class B staged facts that have reached confirmed_count >= threshold.
    Inserts into facts table, marks staged row as promoted.
    Returns count of promoted facts.
    """
    promoted = 0
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, subject_id, object_id, rel_type,
                       provenance, confidence
                FROM staged_facts
                WHERE fact_class = 'B'
                  AND confirmed_count >= %s
                  AND promoted_at IS NULL
                  AND expires_at > now()
                """,
                (promotion_threshold,)
            )
            candidates = cur.fetchall()

        for row in candidates:
            sid, user_id, subject, obj, rel_type, prov, conf = row
            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO facts"
                        " (user_id, subject_id, object_id, rel_type, provenance,"
                        "  confidence, fact_class, fact_provenance, qdrant_synced)"
                        " VALUES (%s, %s, %s, %s, %s, %s, 'B', %s, false)"
                        " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now()",
                        (user_id, subject, obj, rel_type, prov, conf, prov)
                    )
                    cur.execute(
                        "UPDATE staged_facts SET promoted_at = now() WHERE id = %s",
                        (sid,)
                    )
                db_conn.commit()
                promoted += 1

                # Best-effort: delete staged Qdrant point after promotion commits
                try:
                    collection = derive_collection(user_id)
                    httpx.post(
                        f"{qdrant_url}/collections/{collection}/points/delete",
                        json={"points": [sid]},
                        timeout=5.0
                    )
                except Exception as e:
                    log.warning(f"Failed to delete staged Qdrant point {sid} after promotion: {e}")

                log.info(
                    f"re_embedder.promoted fact staged_id={sid} "
                    f"subject={subject} rel_type={rel_type}"
                )
            except Exception as e:
                db_conn.rollback()
                log.error(f"re_embedder.promote_failed staged_id={sid}: {e}")

    except Exception as e:
        log.error(f"re_embedder.promote_staged_error: {e}")

    return promoted


def expire_staged_facts(db_conn, qdrant_url: str) -> int:
    """
    Delete Class C staged facts past their expires_at.
    Also deletes their Qdrant vectors.
    Returns count of expired facts.
    """
    expired = 0
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id FROM staged_facts
                WHERE expires_at <= now()
                  AND promoted_at IS NULL
                """
            )
            stale = cur.fetchall()

        for staged_id, user_id in stale:
            collection = derive_collection(user_id)
            try:
                httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/delete",
                    json={"points": [staged_id]},
                    timeout=10.0,
                )
            except Exception:
                pass  # Best effort Qdrant cleanup

            try:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM staged_facts WHERE id = %s",
                        (staged_id,)
                    )
                db_conn.commit()
                expired += 1
                log.info(f"re_embedder.expired staged_id={staged_id} user_id={user_id}")
            except Exception as e:
                db_conn.rollback()
                log.error(f"re_embedder.expire_failed staged_id={staged_id}: {e}")

    except Exception as e:
        log.error(f"re_embedder.expire_staged_error: {e}")

    return expired


def reconcile_qdrant(db_conn, qdrant_url: str, qwen_api_url: str) -> dict:
    """
    Full reconciliation pass across all FaultLine Qdrant collections.
    Scrolls all points, compares payloads to PostgreSQL ground truth,
    deletes orphaned/superseded points, re-upserts diverged payloads.
    Returns: {"deleted": int, "reupserted": int, "ok": int, "errors": int}
    """
    stats = {"deleted": 0, "reupserted": 0, "ok": 0, "errors": 0}

    # Step 1: Discover all FaultLine collections
    try:
        response = httpx.get(f"{qdrant_url}/collections", timeout=10.0)
        response.raise_for_status()
        data = response.json()
        collections = [
            c["name"] for c in data.get("result", {}).get("collections", [])
            if c["name"].startswith("faultline-")
        ]
    except Exception as e:
        log.error(f"re_embedder.reconcile_discover_failed: {e}")
        return stats

    if not collections:
        log.info("re_embedder.reconcile no collections found")
        return stats

    log.info(f"re_embedder.reconcile_start collections={len(collections)}")

    # Process each collection
    for collection in collections:
        try:
            # Step 2: Scroll all points with payload but no vectors
            all_points = []
            next_page_offset = None

            while True:
                scroll_body = {
                    "limit": 250,
                    "with_payload": True,
                    "with_vector": False,
                }
                if next_page_offset is not None:
                    scroll_body["offset"] = next_page_offset

                response = httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/scroll",
                    json=scroll_body,
                    timeout=10.0
                )
                response.raise_for_status()
                data = response.json()

                points = data.get("result", {}).get("points", [])
                all_points.extend(points)

                next_page_offset = data.get("result", {}).get("next_page_offset")
                if next_page_offset is None:
                    break

            if not all_points:
                continue

            log.info(f"re_embedder.reconcile_scroll collection={collection} count={len(all_points)}")

            # Step 3: Batch fetch PostgreSQL ground truth
            fact_ids = [
                p["payload"]["fact_id"] for p in all_points
                if "fact_id" in p.get("payload", {})
            ]

            if not fact_ids:
                continue

            pg_facts = {}
            with db_conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(fact_ids))
                cur.execute(
                    f"""
                    SELECT id, user_id, subject_id, object_id, rel_type, provenance,
                           confidence, confirmed_count, last_seen_at, contradicted_by,
                           hard_delete_flag, superseded_at
                    FROM facts
                    WHERE id IN ({placeholders})
                    """,
                    fact_ids
                )
                for row in cur.fetchall():
                    pg_facts[row[0]] = {
                        "id": row[0],
                        "user_id": row[1],
                        "subject_id": row[2],
                        "object_id": row[3],
                        "rel_type": row[4],
                        "provenance": row[5],
                        "confidence": row[6],
                        "confirmed_count": row[7],
                        "last_seen_at": row[8],
                        "contradicted_by": row[9],
                        "hard_delete_flag": row[10],
                        "superseded_at": row[11],
                    }

            # Step 4: Reconcile each point
            for point in all_points:
                try:
                    point_id = point["id"]
                    payload = point.get("payload", {})
                    fact_id = payload.get("fact_id")

                    # 4a: Check if fact exists in PostgreSQL
                    if fact_id not in pg_facts:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json={"points": [point_id]},
                            timeout=10.0
                        )
                        stats["deleted"] += 1
                        log.info(f"re_embedder.reconcile_deleted point_id={point_id} reason=not_in_pg collection={collection}")
                        continue

                    pg_row = pg_facts[fact_id]

                    # 4b: Check if fact is superseded or hard-deleted
                    if pg_row["superseded_at"] is not None or pg_row["hard_delete_flag"]:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json={"points": [point_id]},
                            timeout=10.0
                        )
                        stats["deleted"] += 1
                        reason = "hard_deleted" if pg_row["hard_delete_flag"] else "superseded"
                        log.info(f"re_embedder.reconcile_deleted point_id={point_id} reason={reason} collection={collection}")
                        continue

                    # 4c: Build expected payload from PostgreSQL ground truth
                    resolved_rows = resolve_display_names_for_facts(db_conn, [pg_row])
                    resolved_row = resolved_rows[0]

                    expected_payload = {
                        "subject": resolved_row.get("subject_display", pg_row["subject_id"]),
                        "object": resolved_row.get("object_display", pg_row["object_id"]),
                        "rel_type": pg_row["rel_type"],
                        "confidence": pg_row["confidence"],
                        "confirmed_count": pg_row["confirmed_count"],
                        "contradicted": pg_row["contradicted_by"] is not None,
                    }

                    # 4d: Compare payloads (exclude last_seen_at and other fields)
                    # Use tolerance for confidence (JSON float round-trip drift)
                    payload_matches = (
                        payload.get("subject") == expected_payload["subject"]
                        and payload.get("object") == expected_payload["object"]
                        and payload.get("rel_type") == expected_payload["rel_type"]
                        and abs((payload.get("confidence") or 0.0) - expected_payload["confidence"]) <= 0.001
                        and payload.get("confirmed_count") == expected_payload["confirmed_count"]
                        and payload.get("contradicted") == expected_payload["contradicted"]
                    )

                    if not payload_matches:
                        # 4e: Re-embed and re-upsert
                        text = f"{expected_payload['subject']} {expected_payload['rel_type']} {expected_payload['object']}"
                        vector = embed_text(text, qwen_api_url, timeout=30.0, fallback=True)
                        if upsert_to_qdrant(resolved_row, vector, collection, qdrant_url):
                            stats["reupserted"] += 1
                            log.info(f"re_embedder.reconcile_reupserted point_id={point_id} fact_id={fact_id} collection={collection}")
                        else:
                            stats["errors"] += 1
                    else:
                        # 4f: Payload matches
                        stats["ok"] += 1

                except Exception as e:
                    fact_id = point.get("payload", {}).get("fact_id", "unknown")
                    stats["errors"] += 1
                    log.error(f"re_embedder.reconcile_point_error point_id={point.get('id')} fact_id={fact_id} collection={collection}: {e}")

        except Exception as e:
            log.error(f"re_embedder.reconcile_collection_error collection={collection}: {e}")

    return stats


def evaluate_ontology_candidates(db_conn, qwen_api_url: str) -> dict:
    """
    dprompt-17: Evaluate novel rel_type candidates from ontology_evaluations.
    Runs each poll cycle. Decisions:
      - 'approved': occurrence_count >= 3 → INSERT into rel_types
      - 'mapped':   similarity to existing type > 0.85 → rewrite staged_facts
      - 'rejected': neither → leave as Class C, let expiry handle it

    Returns: {"approved": int, "mapped": int, "rejected": int, "errors": int}
    """
    stats = {"approved": 0, "mapped": 0, "rejected": 0, "errors": 0}

    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, candidate_rel_type, candidate_subject_type,"
                "       candidate_object_type, first_text_snippet, occurrence_count,"
                "       sample_subject_id, sample_object"
                " FROM ontology_evaluations"
                " WHERE re_embedder_decision IS NULL"
                " ORDER BY occurrence_count DESC, last_seen_at DESC"
            )
            candidates = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.ontology_eval_fetch_failed: {e}")
        return stats

    if not candidates:
        return stats

    log.info(f"re_embedder.ontology_eval_candidates count={len(candidates)}")

    # Load existing rel_types for similarity comparison
    try:
        with db_conn.cursor() as cur:
            cur.execute("SELECT rel_type FROM rel_types ORDER BY rel_type")
            existing_types = [row[0] for row in cur.fetchall()]
    except Exception:
        existing_types = []

    for row in candidates:
        eval_id, user_id, candidate_rel, subj_type, obj_type, snippet, occ, subj_id, obj = row
        try:
            decision = None
            reason = ""
            best_fit = None
            best_score = 0.0

            # ── Decision 1: Pattern frequency ──────────────────────────
            if occ >= 3:
                decision = "approved"
                reason = f"occurrence_count={occ} >= 3"

            # ── Decision 2: Semantic similarity ─────────────────────────
            if not decision and existing_types:
                # Embed the candidate rel_type for comparison
                candidate_vector = embed_text(
                    f"relationship: {candidate_rel}",
                    qwen_api_url, timeout=10.0, fallback=True
                )
                if candidate_vector:
                    # Compute cosine similarity to each existing type
                    best_score = 0.0
                    best_fit = None
                    for ext in existing_types:
                        ext_vector = embed_text(
                            f"relationship: {ext}",
                            qwen_api_url, timeout=10.0, fallback=True
                        )
                        if ext_vector:
                            sim = _cosine_similarity(candidate_vector, ext_vector)
                            if sim > best_score:
                                best_score = sim
                                best_fit = ext

                    if best_score > 0.85 and best_fit:
                        decision = "mapped"
                        reason = f"similarity={best_score:.3f} to '{best_fit}'"

            # ── Decision 3: Reject ──────────────────────────────────────
            if not decision:
                decision = "rejected"
                reason = f"occ={occ} < 3, no strong match (best={best_fit}:{best_score:.3f})" if best_fit else "no match"

            # ── Apply decision ──────────────────────────────────────────
            with db_conn.cursor() as cur:
                if decision == "approved":
                    # Register the new rel_type with inferred metadata (dprompt-97)
                    label = candidate_rel.replace('_', ' ').title()

                    # Infer metadata from candidate's subject and object types
                    head_types = None
                    tail_types = None
                    is_hierarchy = False
                    is_symmetric = False

                    if subj_type and subj_type != "unknown":
                        head_types = [subj_type]
                    if obj_type and obj_type != "unknown":
                        tail_types = [obj_type]

                    # Heuristic: if rel_type suggests classification/taxonomy, mark as hierarchy
                    if any(keyword in candidate_rel.lower() for keyword in ("instance_of", "subclass_of", "member_of", "is_a", "part_of", "type_of")):
                        is_hierarchy = True
                    # Heuristic: if suggests symmetry
                    if any(keyword in candidate_rel.lower() for keyword in ("friend", "knows", "same", "peer", "mutual", "colleague")):
                        is_symmetric = True

                    cur.execute(
                        "INSERT INTO rel_types"
                        " (rel_type, label, engine_generated, confidence, source,"
                        "  head_types, tail_types, is_hierarchy_rel, is_symmetric)"
                        " VALUES (%s, %s, true, 0.7, 're_embedder', %s, %s, %s, %s)"
                        " ON CONFLICT (rel_type) DO NOTHING",
                        (candidate_rel, label, head_types, tail_types, is_hierarchy, is_symmetric),
                    )
                    stats["approved"] += 1
                    log.info(f"re_embedder.ontology_approved rel_type={candidate_rel} head_types={head_types} tail_types={tail_types} is_hierarchy={is_hierarchy} {reason}")

                    # Refresh unified metadata cache so the newly approved rel_type
                    # is immediately available to the ingest pipeline without waiting
                    # for next container restart (dprompt-76b / dBug-015).
                    try:
                        from src.api.main import _refresh_rel_type_cache
                        _refresh_rel_type_cache()
                        log.info(f"re_embedder.cache_refresh trigger=ontology_approved rel_type={candidate_rel}")
                    except Exception as _cache_err:
                        log.warning(f"re_embedder.cache_refresh_failed rel_type={candidate_rel}: {_cache_err}")

                elif decision == "mapped" and best_fit:
                    # Rewrite staged_facts using this rel_type to use best_fit instead
                    cur.execute(
                        "UPDATE staged_facts SET rel_type = %s, qdrant_synced = false"
                        " WHERE rel_type = %s AND promoted_at IS NULL AND expires_at > now()",
                        (best_fit, candidate_rel),
                    )
                    n_rewritten = cur.rowcount
                    stats["mapped"] += 1
                    log.info(
                        f"re_embedder.ontology_mapped "
                        f"from={candidate_rel} to={best_fit} "
                        f"rewritten={n_rewritten} score={best_score:.3f}"
                    )

                else:
                    stats["rejected"] += 1
                    log.info(f"re_embedder.ontology_rejected rel_type={candidate_rel} {reason}")

                # Update evaluation record
                cur.execute(
                    "UPDATE ontology_evaluations SET"
                    "  re_embedder_decision = %s,"
                    "  re_embedder_confidence = %s,"
                    "  decision_timestamp = now(),"
                    "  decision_reason = %s,"
                    "  best_fit_rel_type = %s,"
                    "  best_fit_score = %s,"
                    "  created_rel_type = %s"
                    " WHERE id = %s",
                    (decision,
                     0.7 if decision == "approved" else (best_score if decision == "mapped" else 0.3),
                     reason, best_fit, best_score,
                     candidate_rel if decision == "approved" else (best_fit if decision == "mapped" else None),
                     eval_id),
                )

            db_conn.commit()

        except Exception as e:
            db_conn.rollback()
            stats["errors"] += 1
            log.error(f"re_embedder.ontology_eval_error eval_id={eval_id} rel_type={candidate_rel}: {e}")

    return stats


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def main():
    """Main poll loop."""
    postgres_dsn = os.getenv("POSTGRES_DSN")
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    qwen_api_url = os.getenv("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
    interval = int(os.getenv("REEMBED_INTERVAL", "10"))
    confidence_threshold = float(os.getenv("QDRANT_SYNC_CONFIDENCE_THRESHOLD", "0.0"))

    if not postgres_dsn:
        log.error("POSTGRES_DSN not configured")
        return

    log.info(f"re_embedder.start interval={interval}s qdrant_url={qdrant_url} confidence_threshold={confidence_threshold}")

    while True:
        try:
            # At the top of every iteration, before any DB query, ensure the default
            # collection exists. This recovers a deleted collection within one loop
            # cycle regardless of whether there are any unsynced rows.
            default_collection = os.getenv("QDRANT_COLLECTION", "faultline-test")
            ensure_collection(default_collection, qdrant_url)

            db = psycopg2.connect(postgres_dsn)
            try:
                rows = fetch_unsynced(db, confidence_threshold)

                if rows:
                    log.info(f"re_embedder.batch_start count={len(rows)}")

                    # Ensure every per-user collection needed by this batch exists
                    # before processing any rows.
                    seen_collections: set[str] = {default_collection}
                    for row in rows:
                        col = derive_collection(row["user_id"])
                        if col not in seen_collections:
                            seen_collections.add(col)
                            if not ensure_collection(col, qdrant_url):
                                log.error(f"re_embedder.collection_unavailable collection={col}")

                    # Resolve display names for all rows in batch before embedding
                    rows = resolve_display_names_for_facts(db, rows)

                for row in rows:
                    try:
                        text = f"{row['subject_display']} {row['rel_type']} {row['object_display']}"
                        vector = embed_text(text, qwen_api_url)
                        collection = derive_collection(row["user_id"])

                        if upsert_to_qdrant(row, vector, collection, qdrant_url):
                            mark_synced(db, row["id"])
                            log.info(
                                f"re_embedder.synced fact_id={row['id']} "
                                f"collection={collection} "
                                f"subject={row['subject_id']} "
                                f"object={row['object_id']}"
                            )

                    except Exception as e:
                        log.error(f"re_embedder.row_error fact_id={row['id']}: {e}")
                        continue

                # Process staged facts — sync to Qdrant
                staged_rows = fetch_unsynced_staged(db)
                if staged_rows:
                    staged_rows = resolve_display_names_for_facts(db, staged_rows)
                    log.info(f"re_embedder.staged_batch count={len(staged_rows)}")
                    for row in staged_rows:
                        try:
                            text = f"{row['subject_display']} {row['rel_type']} {row['object_display']}"
                            vector = embed_text(text, qwen_api_url)
                            collection = derive_collection(row["user_id"])
                            if upsert_to_qdrant(row, vector, collection, qdrant_url):
                                with db.cursor() as cur:
                                    cur.execute(
                                        "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                        (row["staged_id"],)
                                    )
                                db.commit()
                        except Exception as e:
                            log.error(f"re_embedder.staged_row_error id={row['staged_id']}: {e}")

                # Promote eligible Class B staged facts to long-term memory
                n_promoted = promote_staged_facts(db, qdrant_url)
                if n_promoted:
                    log.info(f"re_embedder.promotion_complete promoted={n_promoted}")

                # Evaluate novel ontology candidates (dprompt-17)
                ontology_stats = evaluate_ontology_candidates(db, qwen_api_url)
                if any(v > 0 for v in ontology_stats.values()):
                    log.info(
                        f"re_embedder.ontology_eval "
                        f"approved={ontology_stats['approved']} "
                        f"mapped={ontology_stats['mapped']} "
                        f"rejected={ontology_stats['rejected']} "
                        f"errors={ontology_stats['errors']}"
                    )

                # Expire stale Class C staged facts
                n_expired = expire_staged_facts(db, qdrant_url)
                if n_expired:
                    log.info(f"re_embedder.expiry_complete expired={n_expired}")

                # Deletion pass — remove superseded facts from Qdrant
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT id, user_id FROM facts "
                        "WHERE superseded_at IS NOT NULL AND qdrant_synced = false"
                    )
                    superseded = cur.fetchall()

                for fact_id, uid in superseded:
                    collection = derive_collection(uid)
                    try:
                        resp = httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json={"points": [fact_id]},
                            timeout=10.0,
                        )
                        if resp.status_code in (200, 404):
                            with db.cursor() as cur:
                                cur.execute(
                                    "UPDATE facts SET qdrant_synced = true WHERE id = %s",
                                    (fact_id,),
                                )
                            db.commit()
                            log.info(
                                "re_embedder.deleted",
                                fact_id=fact_id,
                                collection=collection,
                            )
                    except Exception as e:
                        log.error(f"re_embedder.delete_failed fact_id={fact_id}: {e}")

                # Hard delete pass — remove hard-deleted facts from Qdrant
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT id, user_id FROM facts "
                        "WHERE hard_delete_flag = true AND qdrant_synced = false"
                    )
                    hard_deleted = cur.fetchall()

                for fact_id, uid in hard_deleted:
                    collection = derive_collection(uid)
                    try:
                        resp = httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json={"points": [fact_id]},
                            timeout=10.0,
                        )
                        if resp.status_code in (200, 404):
                            with db.cursor() as cur:
                                cur.execute(
                                    "UPDATE facts SET qdrant_synced = true WHERE id = %s",
                                    (fact_id,),
                                )
                            db.commit()
                            log.info(f"re_embedder.hard_deleted fact_id={fact_id} collection={collection}")
                    except Exception as e:
                        log.error(f"re_embedder.hard_delete_failed fact_id={fact_id}: {e}")

                # Reconciliation pass — sync stale payloads and orphaned points
                stats = reconcile_qdrant(db, qdrant_url, qwen_api_url)
                if any(v > 0 for v in [stats["deleted"], stats["reupserted"], stats["errors"]]):
                    log.info(f"re_embedder.reconcile deleted={stats['deleted']} reupserted={stats['reupserted']} ok={stats['ok']} errors={stats['errors']}")

            finally:
                db.close()

        except Exception as e:
            log.error(f"re_embedder.loop_error: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
