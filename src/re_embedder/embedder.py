"""
FaultLine Re-Embedder Service

Polls the facts table for unsynced rows, embeds them, and upserts to per-user Qdrant collections.
This is the only service that writes to Qdrant.
"""
import atexit
import hashlib
import json
import logging
import os
import time
from typing import Optional

import httpx
import psycopg2
import redis
from src.api.llm_client import get_llm_headers
from src.api.llm_calls import (
    call_llm_with_retry_sync,
    close_llm_http_client,
    LLMTimeouts,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger(__name__)

# Embedding model name — overridable via env var
_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")

# Lazy-loaded local embedder — initialized on first use, None if fastembed not installed
_local_embedder = None


def _get_local_embedder():
    global _local_embedder
    if _local_embedder is not None:
        return _local_embedder
    try:
        from fastembed import TextEmbedding
        _local_embedder = TextEmbedding("nomic-ai/nomic-embed-text-v1.5")
        log.info("local_embedder.initialized model=nomic-embed-text-v1.5")
    except ImportError:
        log.warning("local_embedder.unavailable fastembed not installed")
        _local_embedder = False   # sentinel: don't retry import
    return _local_embedder


# Global pooled HTTP client for embedding and Qdrant calls (dBug-051 fix)
# Prevents connection churn from bare httpx.post() calls
_http_client = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=10))


def _get_circuit_breaker_status() -> dict:
    """Get circuit breaker status for LLM calls (for awareness in background loop).

    Returns:
        Dict with 'is_open' key indicating if circuit breaker is active
    """
    try:
        from src.api.llm_calls import _llm_circuit_breaker
        return {"is_open": _llm_circuit_breaker.is_open()}
    except Exception:
        # If import fails, assume circuit is closed (normal operation)
        return {"is_open": False}

# Marker for internal FaultLine prompts (dprompt-128) — prevents context bloat if looped back
_FAULTLINE_INTERNAL_PREFIX = "[FaultLine-Internal]"

_http_client_sync: httpx.Client = None


def _detect_redis_endpoint() -> str:
    """Auto-detect Redis endpoint (container-aware).

    Priority chain:
    1. REDIS_URL env var override
    2. Docker service name (redis) on default port 6379
    3. Localhost (dev fallback)

    This allows the same code to work in Docker containers (service name)
    and local dev environments (localhost).
    """
    # Explicit override
    if os.getenv("REDIS_URL"):
        return os.getenv("REDIS_URL")

    # Docker service name (most likely in container)
    candidates = [
        "redis://redis:6379/0",           # Docker service name (most reliable)
        "redis://localhost:6379/0",       # Local development fallback
        "redis://127.0.0.1:6379/0",       # Localhost IPv4 fallback
    ]

    for url in candidates:
        try:
            test_client = redis.from_url(url, decode_responses=True, socket_timeout=2)
            test_client.ping()
            log.info(f"redis_detection.success url={url[:30]}")
            return url
        except Exception:
            continue

    # If all fail, return Docker service name (will be retried with exponential backoff)
    log.warning("redis_detection.all_failed using_service_name=redis")
    return "redis://redis:6379/0"


class EmbeddingCache:
    """Redis-backed cache for rel_type embeddings (GROWS WITH SYSTEM).

    Caches embeddings of rel_type name strings to avoid re-embedding during
    ontology evaluation. Survives restarts, scales horizontally.
    """

    def __init__(self, redis_url: Optional[str] = None):
        """Initialize Redis connection for embedding cache.

        Args:
            redis_url: Redis connection URL (auto-detects if not provided)
        """
        self.redis_url = redis_url or _detect_redis_endpoint()
        self.ttl = int(os.getenv("EMBEDDING_CACHE_TTL", "86400"))  # 1 day default
        self.prefix = "embedding:relationship:"
        self.client = None

        try:
            self.client = redis.from_url(self.redis_url, decode_responses=True)
            self.client.ping()
            log.info(f"embedding_cache.redis_connected url={self.redis_url[:30]} ttl_seconds={self.ttl}")
        except Exception as e:
            log.warning(f"embedding_cache.redis_connection_failed error={str(e)}")
            self.client = None

    def get(self, text: str) -> Optional[list]:
        """Retrieve cached embedding (returns None on miss or error)."""
        if not self.client:
            return None
        try:
            key = f"{self.prefix}{text}"
            cached = self.client.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            log.warning("embedding_cache.get_error", error=str(e), key=text[:40])
        return None

    def set(self, text: str, vector: list) -> bool:
        """Cache an embedding with TTL (returns success flag)."""
        if not self.client:
            return False
        try:
            key = f"{self.prefix}{text}"
            self.client.setex(key, self.ttl, json.dumps(vector))
            return True
        except Exception as e:
            log.warning("embedding_cache.set_error", error=str(e), key=text[:40])
            return False

    def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern (e.g., 'embedding:relationship:*')."""
        if not self.client:
            return 0
        try:
            # Use SCAN to avoid blocking on large keyspaces
            deleted = 0
            cursor = 0
            while True:
                cursor, keys = self.client.scan(cursor, match=pattern, count=1000)
                if keys:
                    deleted += self.client.delete(*keys)
                if cursor == 0:
                    break
            return deleted
        except Exception as e:
            log.warning("embedding_cache.clear_error", error=str(e), pattern=pattern)
            return 0


_embedding_cache = EmbeddingCache()


def derive_collection(user_id: str) -> str:
    """Derive Qdrant collection name from user_id."""
    if user_id in ("", "anonymous", "legacy"):
        return os.getenv("QDRANT_COLLECTION", "faultline-test")
    return f"faultline-{user_id}"


def fetch_unsynced(db_conn, user_id: str, confidence_threshold: float = 0.0) -> list[dict]:
    """Fetch all non-superseded facts where qdrant_synced = false and confidence >= threshold.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, subject_id, object_id, rel_type, provenance,
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
            "user_id": user_id,
            "confidence": row[5] if row[5] is not None else 1.0,
            "confirmed_count": row[6] if row[6] is not None else 0,
            "last_seen_at": row[7],
            "contradicted_by": row[8],
        }
        for row in rows
    ]


def fetch_unsynced_staged(db_conn, user_id: str) -> list[dict]:
    """Fetch staged_facts where qdrant_synced = false and not yet promoted or expired.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject_id, object_id, rel_type, provenance,
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
            "user_id": user_id,
            "confidence": row[5] if row[5] is not None else 0.6,
            "confirmed_count": row[6] if row[6] is not None else 0,
            "last_seen_at": row[7],
            "contradicted_by": None,
            "staged_id": row[0],
            "fact_class": row[8],
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


def embed_text(text: str, qwen_api_url: str, timeout: float = 30.0, fallback: bool = True, embedding_url: str = None) -> list[float] | None:
    """
    Embed text using the nomic-embed-text model via the Ollama/Qwen API.

    fallback=True  (default, used by re_embedder): returns a hash vector on failure so
                   the re_embedder loop keeps running.
    fallback=False (used by /query):               returns None on failure so the caller
                   can skip the Qdrant search rather than searching with a meaningless vector.
    embedding_url: Explicit embedding endpoint (optional, overrides inferred path).
    """
    # Use explicit embedding URL if provided, fallback to inferring from chat URL
    if embedding_url:
        embed_url = embedding_url
    else:
        embed_url = qwen_api_url.replace("/chat/completions", "/embeddings")

    try:
        # Use persistent pooled client if available, fallback to httpx.post() for bac${LOCATION}ard compatibility
        if _http_client_sync:
            response = _http_client_sync.post(
                embed_url,
                json={"model": _EMBEDDING_MODEL, "input": text},
                headers=get_llm_headers(),
                timeout=timeout,
            )
        else:
            # Use pooled client instead of bare httpx.post() (dBug-051: prevent connection churn)
            response = _http_client.post(
                embed_url,
                json={"model": _EMBEDDING_MODEL, "input": text},
                headers=get_llm_headers(),
                timeout=timeout,
            )
        response.raise_for_status()
        data = response.json()

        if "data" in data and len(data["data"]) > 0:
            return data["data"][0]["embedding"]

        raise ValueError("Invalid embedding response format")

    except Exception as e:
        if fallback:
            # Try local CPU embedder before falling back to hash vector
            local = _get_local_embedder()
            if local:
                try:
                    vectors = list(local.embed([text]))
                    if vectors:
                        log.info("local_embedder.used text_preview={}", text[:50])
                        return list(vectors[0])
                except Exception as local_err:
                    log.warning(f"local_embedder.failed: {local_err}")
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
    Check if Qdrant collection exists with the correct anonymous vector schema, create or
    recreate if not.

    Validates that an existing collection uses anonymous-vector schema
    {"size": 768, "distance": "Cosine"}.  Collections pre-created by OpenWebUI or other
    tools use named-vector schema ({"vectors": {}}) which causes Qdrant to return 400 on
    every bare-list search.  When a schema mismatch is detected the collection is deleted
    and recreated with the correct schema.

    Returns True if collection exists with correct schema or was created/recreated.
    Returns False on any unrecoverable failure.
    """
    _EXPECTED_DIM = 768
    _CORRECT_SCHEMA = {"size": _EXPECTED_DIM, "distance": "Cosine"}

    def _create_collection() -> bool:
        """PUT the collection with the correct anonymous-vector schema."""
        create_response = httpx.put(
            f"{qdrant_url}/collections/{collection}",
            json={"vectors": _CORRECT_SCHEMA},
            timeout=10.0,
        )
        if create_response.status_code == 200:
            log.info(f"re_embedder.collection_created collection={collection}")
            return True
        log.error(
            f"re_embedder.collection_create_failed collection={collection} "
            f"status={create_response.status_code}"
        )
        return False

    try:
        # Use pooled client instead of bare httpx.get() (dBug-051: prevent connection churn)
        response = _http_client.get(
            f"{qdrant_url}/collections/{collection}",
            timeout=10.0
        )

        if response.status_code == 200:
            # Validate that the existing collection uses anonymous-vector schema.
            # OpenWebUI may pre-create collections with named-vector schema ("vectors": {})
            # which causes Qdrant to return 400 on bare-list searches.
            try:
                body = response.json()
                vectors_cfg = (
                    body.get("result", {})
                        .get("config", {})
                        .get("params", {})
                        .get("vectors", None)
                )
            except Exception as parse_err:
                log.warning(
                    f"re_embedder.collection_schema_parse_failed collection={collection} "
                    f"error={parse_err} — treating as valid to avoid data loss"
                )
                return True

            # Anonymous-vector schema: vectors_cfg is a dict with a top-level "size" key.
            schema_ok = (
                isinstance(vectors_cfg, dict)
                and vectors_cfg.get("size") == _EXPECTED_DIM
            )

            if schema_ok:
                return True

            # Schema mismatch — log, delete, recreate.
            log.warning(
                f"re_embedder.collection_schema_mismatch "
                f"collection={collection} "
                f"found={vectors_cfg!r} "
                f"expected=\"anonymous {_EXPECTED_DIM}-dim cosine\""
            )

            delete_response = _http_client.delete(
                f"{qdrant_url}/collections/{collection}",
                timeout=10.0,
            )
            if delete_response.status_code not in (200, 404):
                log.error(
                    f"re_embedder.collection_delete_failed collection={collection} "
                    f"status={delete_response.status_code} — cannot recreate"
                )
                return False

            result = _create_collection()
            if result:
                log.info(
                    f"re_embedder.collection_recreated_after_schema_fix "
                    f"collection={collection}"
                )
            return result

        if response.status_code == 404:
            return _create_collection()

        log.error(
            f"re_embedder.collection_check_unexpected collection={collection} "
            f"status={response.status_code}"
        )
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
        # Use persistent pooled client if available, fallback to httpx.put() for bac${LOCATION}ard compatibility
        if _http_client_sync:
            response = _http_client_sync.put(
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
        else:
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


def promote_staged_facts(db_conn, qdrant_url: str, user_id: str = None, schema_name: str = None, promotion_threshold: int = 3) -> int:
    """
    Promote Class B staged facts to facts table when confirmed_count >= threshold.

    Confirmation Mechanism (Source: _commit_staged in main.py, lines 1017-1075):
    ────────────────────────────────────────────────────────────────────────────
    Staged facts accumulate a confirmed_count every time they're re-ingested.
    The count increments via PostgreSQL ON CONFLICT clauses in main.py _commit_staged().

    When confirmed_count >= promotion_threshold (default: 3):
    • Fact has appeared in >= 4 separate ingest calls (calls 0→1→2→3)
    • System confidence increases with each occurrence
    • promote_staged_facts() moves to facts table with Class A confidence
    • Staged row marked as promoted (non-destructive soft delete via promoted_at)

    Full Workflow:
    1. Fact inserted 4 times → confirmed_count increments: 0→1→2→3
    2. Re-embedder polls every 60 seconds → calls promote_staged_facts()
    3. Queries: SELECT ... FROM staged_facts WHERE confirmed_count >= 3
    4. For each candidate: INSERT into facts table with ON CONFLICT (increments facts.confirmed_count)
    5. UPDATE staged_facts SET promoted_at = now() (marks row as promoted)
    6. DELETE from Qdrant staged collection (cleanup, best-effort)
    7. Log confirmation to observability system

    Threshold Rationale (3 confirmations):
    • 1 occurrence: Could be typo, one-off phrasing, random utterance
    • 2 occurrences: Still vulnerable to coincidence or misunderstanding
    • 3 occurrences: Sweet spot — filters noise, captures recurring patterns
    • Higher threshold: Would miss important recurring facts, delay promotion

    Edge Cases & Safety:
    • Promotion is **non-cascading** — only the matching triple is promoted
    • Other facts about same entities unaffected (e.g., promoting "works_for" doesn't touch "spouse")
    • Per-user isolation maintained (no cross-user promotion, schema_name isolates via search_path)
    • Archived facts (superseded_at IS NOT NULL) excluded from promotion (WHERE clause)
    • Race-safe: PostgreSQL ACID guarantees atomicity between concurrent ingest calls

    Args:
        db_conn: PostgreSQL connection (per-user schema context via search_path)
        qdrant_url: Qdrant service URL for staged collection cleanup
        user_id: User UUID for collection naming (optional, derived from schema context)
        schema_name: User schema name (e.g., "faultline_christopher"). If provided, sets search_path.
        promotion_threshold: Confirmed count threshold for promotion (default 3, configurable)

    Returns:
        Count of promoted facts successfully moved to facts table.

    Grounding Documents:
    • Self_Growth.md Section "Phase 2: Confirmation Tracking" (Mechanism #9, lines 1060-1100)
    • main.py _commit_staged() (lines 1017-1075, ON CONFLICT confirmation increment)
    • CLAUDE.md "Ingest Pipeline: Three-Stage Intent-Aware Pipeline" (staged facts lifecycle)
    • CLAUDE.md "Fact Classification, Storage & Retrieval" (Class A/B/C promotion flow)
    """
    promoted = 0
    try:
        # CRITICAL: Set search_path per-user schema if schema_name provided
        if schema_name:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}, public")
            except Exception as e:
                log.warning(f"re_embedder.search_path_setup_failed schema={schema_name}: {e}")
                # Continue with current search_path

        # C→B upgrade: Class C facts that have accumulated enough confirmations graduate
        # to Class B and enter the B→facts promotion pipeline in this same cycle.
        # expires_at extended so they survive long enough to reach the next threshold.
        try:
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE staged_facts
                    SET fact_class   = 'B',
                        expires_at   = GREATEST(expires_at, now() + interval '30 days'),
                        qdrant_synced = false
                    WHERE fact_class     = 'C'
                      AND confirmed_count >= %s
                      AND promoted_at IS NULL
                      AND expires_at   > now()
                    """,
                    (promotion_threshold,)
                )
                n_c_to_b = cur.rowcount
            db_conn.commit()
            if n_c_to_b:
                log.info(f"re_embedder.class_c_upgraded_to_b count={n_c_to_b} threshold={promotion_threshold}")
        except Exception as e:
            try:
                db_conn.rollback()
            except Exception:
                pass
            log.error(f"re_embedder.class_c_upgrade_failed: {e}")

        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, subject_id, object_id, rel_type,
                       provenance, confidence
                FROM staged_facts
                WHERE fact_class = 'B'
                  AND confirmed_count >= %s
                  AND promoted_at IS NULL
                """,
                (promotion_threshold,)
            )
            candidates = cur.fetchall()

        for row in candidates:
            sid, subject, obj, rel_type, prov, conf = row
            # user_id is implicit in per-user schema context (set by SET search_path)
            try:
                log.info(
                    f"re_embedder.promoting_staged_fact staged_id={sid}"
                    f" subject_id={subject[:8] if subject else '?'}"
                    f" rel_type={rel_type} threshold={promotion_threshold}"
                )

                with db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO facts"
                        " (subject_id, object_id, rel_type, provenance,"
                        "  confidence, fact_class, fact_provenance, qdrant_synced)"
                        " VALUES (%s, %s, %s, %s, %s, 'B', %s, false)"
                        " ON CONFLICT (subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now()",
                        (subject, obj, rel_type, prov, conf, prov)
                    )
                    cur.execute(
                        "UPDATE staged_facts SET promoted_at = now() WHERE id = %s",
                        (sid,)
                    )
                db_conn.commit()
                promoted += 1

                log.debug(
                    f"re_embedder.promoted_fact_committed staged_id={sid}"
                    f" subject_id={subject[:8] if subject else '?'}"
                    f" rel_type={rel_type} object_id={obj[:8] if obj else '?'}"
                )

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
                try:
                    db_conn.rollback()
                except Exception as rollback_err:
                    log.warning(f"re_embedder.promote_rollback_failed: {rollback_err}")
                log.error(f"re_embedder.promote_failed staged_id={sid}: {e}")

    except Exception as e:
        log.error(f"re_embedder.promote_staged_error: {e}")

    return promoted


def expire_staged_facts(db_conn, qdrant_url: str, user_id: str = None) -> int:
    """
    Score-decay model for staged facts (C and B).

    Every 30-day window without a new confirmation:
      - confirmed_count > 0  → decrement by 1, reset window (+30 days)
      - confirmed_count <= 0 → delete (score hit zero, no evidence to keep)

    This means a fact must be re-observed every ~30 days per point of confidence
    it has accumulated, or it decays back to zero and is removed.

    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    Returns count of facts removed (decayed to zero and deleted).
    """
    expired = 0
    try:
        # Step 1: Decay — Class C facts past their window with remaining score.
        # Class B is long-term memory; once a fact earns B it does not decay.
        # Only Class C (short-term/speculative) participates in the decay cycle.
        with db_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE staged_facts
                SET confirmed_count = confirmed_count - 1,
                    expires_at      = now() + interval '30 days',
                    qdrant_synced   = false
                WHERE fact_class    = 'C'
                  AND expires_at   <= now()
                  AND confirmed_count > 0
                  AND promoted_at IS NULL
                """
            )
            n_decayed = cur.rowcount
        db_conn.commit()
        if n_decayed:
            log.info(f"re_embedder.staged_facts_decayed count={n_decayed} user_id={user_id}")

        # Step 2: Remove — Class C facts at score zero AND past their expiry window.
        # Fresh rows start at confirmed_count=0 but have expires_at = now()+30d — keep them.
        # Only delete when the window has expired AND score is zero (never confirmed or fully decayed).
        # Class B facts are never removed here; retraction handles their lifecycle.
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM staged_facts
                WHERE fact_class    = 'C'
                  AND confirmed_count <= 0
                  AND expires_at   <= now()
                  AND promoted_at IS NULL
                """
            )
            stale = cur.fetchall()

        collection = derive_collection(user_id) if user_id else os.getenv("QDRANT_COLLECTION", "faultline-test")
        for (staged_id,) in stale:
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
                log.info(f"re_embedder.removed staged_id={staged_id} reason=score_zero user_id={user_id}")
            except Exception as e:
                db_conn.rollback()
                log.error(f"re_embedder.expire_failed staged_id={staged_id}: {e}")

    except Exception as e:
        log.error(f"re_embedder.expire_staged_error: {e}")

    return expired


def decay_class_c_hits(db_conn, qdrant_url: str, user_id: str = None, limit: int = 100) -> dict:
    """
    JOB 1 — Class C HIT-LIFECYCLE DECAY sweep (Part B state machine, Part D4).

    Distinct from expire_staged_facts(): that decays the ingest-side `confirmed_count`.
    THIS decays the query-side `hit_count` — the query-hit counter incremented by the
    query path (other agent) on a genuine scoped relevance match. hit_count and
    confirmed_count are NEVER conflated.

    State-machine rule (IDLE 30d, no hit in the window):
        hit_count = hit_count - 1
        expires_at = now() + 30d      # decrement buys another 30-day window
        if hit_count <= 0:  DROP (delete row + best-effort Qdrant point)

    A hit (other agent) pushes expires_at forward, so any row whose expires_at <= now()
    has gone a full window with no hit and is owed a decrement.

    Bounded with LIMIT per cycle so the poll loop stays responsive.
    Returns {"decremented": int, "dropped": int}.
    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    stats = {"decremented": 0, "dropped": 0}
    try:
        # Step 1: Select the idle Class C rows (window elapsed, no hit), bounded.
        # A row with hit_count <= 1 will hit zero on this decrement and must be dropped
        # (need its id + qdrant point), so we select all eligible and branch per-row.
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, hit_count
                FROM staged_facts
                WHERE fact_class  = 'C'
                  AND expires_at <= now()
                  AND promoted_at IS NULL
                ORDER BY expires_at ASC
                LIMIT %s
                """,
                (limit,)
            )
            idle_rows = cur.fetchall()

        if not idle_rows:
            return stats

        collection = derive_collection(user_id) if user_id else os.getenv("QDRANT_COLLECTION", "faultline-test")

        for staged_id, hit_count in idle_rows:
            try:
                new_hits = (hit_count if hit_count is not None else 1) - 1
                if new_hits <= 0:
                    # DROP — best-effort Qdrant delete first (match expiry pattern), then row.
                    try:
                        httpx.post(
                            f"{qdrant_url}/collections/{collection}/points/delete",
                            json={"points": [staged_id]},
                            timeout=10.0,
                        )
                    except Exception:
                        pass  # Best-effort Qdrant cleanup
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM staged_facts WHERE id = %s",
                            (staged_id,)
                        )
                    db_conn.commit()
                    stats["dropped"] += 1
                    log.info(
                        f"re_embedder.class_c_dropped staged_id={staged_id} "
                        f"reason=hit_count_zero user_id={user_id}"
                    )
                else:
                    # Decrement hit_count, reset the 30-day window.
                    with db_conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE staged_facts
                            SET hit_count   = %s,
                                expires_at  = now() + interval '30 days'
                            WHERE id = %s
                            """,
                            (new_hits, staged_id)
                        )
                    db_conn.commit()
                    stats["decremented"] += 1
            except Exception as e:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                log.error(f"re_embedder.class_c_decay_failed staged_id={staged_id}: {e}")

        if stats["decremented"] or stats["dropped"]:
            log.info(
                f"re_embedder.class_c_decay_complete "
                f"decremented={stats['decremented']} dropped={stats['dropped']} user_id={user_id}"
            )

    except Exception as e:
        log.error(f"re_embedder.class_c_decay_error: {e}")

    return stats


def promote_class_c_hits(db_conn, qdrant_url: str, qwen_api_url: str, user_id: str = None,
                         schema_name: str = None, hit_threshold: int = 3, limit: int = 50) -> int:
    """
    JOB 2 — Class C HIT-LIFECYCLE PROMOTION (Part B state machine, Part D4, default B-2).

    When a Class C row reaches hit_count >= 3 (earned via genuine query-scoped hits, NOT
    ingest confirmations), promote it to Class B and route it into the facts table using
    the SAME mechanism as promote_staged_facts() (INSERT ... ON CONFLICT, set promoted_at,
    enqueue Qdrant re-sync, best-effort delete the staged Qdrant point).

    Default B-2 — an UNCLASSIFIED Class C row (rel_type IS NULL, rough memory) is FIRST
    classified at this moment via the existing LLM metadata path
    (_query_llm_for_rel_type_metadata) to derive a structured rel_type + metadata from the
    fact's stored text/context. If classification fails we leave it as Class C and skip
    promotion (do not promote a thing we cannot structure). An already-classified Class C
    just flips to B.

    Bounded with LIMIT per cycle. Fails loud per-row, continues the loop.
    Returns count of rows promoted to facts.
    Per-user schema context: user_id is passed as parameter (schema provides isolation).
    """
    promoted = 0
    try:
        # Optional per-user search_path (mirror promote_staged_facts house style).
        if schema_name:
            try:
                with db_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema_name}, public")
                db_conn.commit()
            except Exception as e:
                log.warning(f"re_embedder.class_c_promote_search_path_failed schema={schema_name}: {e}")
                # Continue with current search_path

        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, subject_id, object_id, rel_type, provenance,
                       confidence, hit_count, rel_type_definition
                FROM staged_facts
                WHERE fact_class = 'C'
                  AND hit_count >= %s
                  AND promoted_at IS NULL
                ORDER BY hit_count DESC
                LIMIT %s
                """,
                (hit_threshold, limit)
            )
            candidates = cur.fetchall()

        if not candidates:
            return promoted

        for row in candidates:
            sid, subject, obj, rel_type, prov, conf, hits, rel_def = row
            required_classification = False
            try:
                # ── Default B-2: classify rough/unclassified memory before promotion ──
                if not rel_type:
                    required_classification = True
                    # Build a candidate rel_type + snippet from stored text/context so the
                    # existing LLM metadata path can derive a structured rel_type. We reuse
                    # the same helper the ontology evaluator uses — no divergent path.
                    resolved = resolve_display_names_for_facts(db_conn, [{
                        "subject_id": subject, "object_id": obj,
                    }])[0]
                    subj_disp = resolved.get("subject_display", subject)
                    obj_disp = resolved.get("object_display", obj)
                    snippet = (rel_def or prov or f"{subj_disp} {obj_disp}").strip()
                    candidate_rel = "related_to"  # rough seed; LLM infers the real rel_type metadata
                    llm_md = _query_llm_for_rel_type_metadata(
                        candidate_rel, "unknown", "unknown", snippet, qwen_api_url
                    )
                    if not llm_md or not llm_md.get("llm_natural_language"):
                        # FAIL LOUD: cannot structure → leave as Class C, do not promote.
                        log.warning(
                            f"re_embedder.class_c_promote_classify_failed staged_id={sid} "
                            f"hit_count={hits} reason=no_llm_metadata — left as Class C"
                        )
                        continue

                    # Register the inferred rel_type into rel_types so the structured fact has
                    # a real ontology entry (mirrors evaluate_ontology_candidates INSERT shape).
                    natural_language = llm_md.get("llm_natural_language", "")
                    is_symmetric = llm_md.get("llm_is_symmetric", False)
                    inverse_rel_type = llm_md.get("llm_inverse_rel_type")
                    category = llm_md.get("llm_category", "other")
                    head_types = llm_md.get("llm_head_types") or ["ANY"]
                    tail_types = llm_md.get("llm_tail_types") or ["ANY"]
                    label = candidate_rel.replace('_', ' ').title()
                    with db_conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO rel_types"
                            " (rel_type, label, natural_language, engine_generated, confidence, source,"
                            "  head_types, tail_types, is_hierarchy_rel, is_symmetric, inverse_rel_type, category, fact_class)"
                            " VALUES (%s, %s, %s, true, %s, 'class_c_promotion', %s, %s, false, %s, %s, %s, 'B')"
                            " ON CONFLICT (rel_type) DO UPDATE SET"
                            "  natural_language = EXCLUDED.natural_language,"
                            "  category = EXCLUDED.category,"
                            "  head_types = CASE WHEN (rel_types.head_types IS NULL"
                            "                          OR rel_types.head_types = ARRAY[]::TEXT[])"
                            "                    THEN EXCLUDED.head_types ELSE rel_types.head_types END,"
                            "  tail_types = CASE WHEN (rel_types.tail_types IS NULL"
                            "                          OR rel_types.tail_types = ARRAY[]::TEXT[])"
                            "                    THEN EXCLUDED.tail_types ELSE rel_types.tail_types END",
                            (candidate_rel, label, natural_language, 0.8, head_types, tail_types,
                             is_symmetric, inverse_rel_type, category),
                        )
                    rel_type = candidate_rel

                # ── Promote C → B via the SAME mechanism as promote_staged_facts() ──
                promote_conf = max(conf if conf is not None else 0.0, 0.6)  # ensure >= 0.6 (Class B floor)
                with db_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO facts"
                        " (subject_id, object_id, rel_type, provenance,"
                        "  confidence, fact_class, fact_provenance, qdrant_synced)"
                        " VALUES (%s, %s, %s, %s, %s, 'B', %s, false)"
                        " ON CONFLICT (subject_id, object_id, rel_type)"
                        " DO UPDATE SET"
                        "   confirmed_count = facts.confirmed_count + 1,"
                        "   last_seen_at    = now(),"
                        "   updated_at      = now()",
                        (subject, obj, rel_type, prov, promote_conf, prov)
                    )
                    cur.execute(
                        "UPDATE staged_facts SET fact_class = 'B', promoted_at = now() WHERE id = %s",
                        (sid,)
                    )
                db_conn.commit()
                promoted += 1

                # Best-effort: delete staged Qdrant point after promotion commits
                # (match promote_staged_facts cleanup pattern). New facts-table point is
                # re-synced next cycle via qdrant_synced=false above.
                try:
                    collection = derive_collection(user_id)
                    httpx.post(
                        f"{qdrant_url}/collections/{collection}/points/delete",
                        json={"points": [sid]},
                        timeout=5.0
                    )
                except Exception as e:
                    log.warning(f"re_embedder.class_c_promote_qdrant_delete_failed staged_id={sid}: {e}")

                log.info(
                    f"re_embedder.class_c_promoted staged_id={sid} rel_type={rel_type} "
                    f"hit_count={hits} required_classification={required_classification} user_id={user_id}"
                )
            except Exception as e:
                try:
                    db_conn.rollback()
                except Exception as rollback_err:
                    log.warning(f"re_embedder.class_c_promote_rollback_failed: {rollback_err}")
                log.error(f"re_embedder.class_c_promote_failed staged_id={sid}: {e}")

    except Exception as e:
        log.error(f"re_embedder.class_c_promote_error: {e}")

    return promoted


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


def _query_llm_for_rel_type_metadata(candidate_rel: str, subj_type: str, obj_type: str,
                                      snippet: str, qwen_api_url: str) -> dict:
    """
    dprompt-126: Phase 2 — Query LLM for natural language metadata during ontology evaluation.

    When a novel rel_type reaches occurrence_count >= 3, query the LLM to generate:
    - natural_language: human-readable description
    - is_symmetric: whether the relationship is bidirectional
    - inverse_rel_type: the opposite relationship (if asymmetric)
    - category: classification (family, work, behavioral, etc.)
    - fact_class: confidence → A/B/C assignment
    - confidence: 0.0-1.0 assessment
    - examples: sample usages for extraction prompt

    Returns dict with llm_* fields or empty dict on failure (non-blocking).

    Phase 3c: Uses call_llm_with_retry_sync() for resilient LLM calls with
    automatic retry and circuit breaker support.
    """
    try:
        # Mark prompt with FaultLine prefix to prevent context bloat if it loops back (dprompt-128)
        prompt = f"""{_FAULTLINE_INTERNAL_PREFIX} You are an ontology expert analyzing a relationship pattern from conversation data.

Pattern: {candidate_rel}
Subject Type: {subj_type or 'unknown'}
Object Type: {obj_type or 'unknown'}
Sample: "{snippet}"

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "natural_language": "X {candidate_rel.replace('_', ' ')} Y  ← MUST use X for subject and Y for object (e.g., 'X and Y are friends', 'X has IP address Y')",
  "is_symmetric": boolean,
  "inverse_rel_type": "opposite rel_type or null",
  "category": "family|work|location|identity|temporal|behavioral|physical|social|network",
  "head_types": ["entity types allowed as SUBJECT, e.g. Person or Object; use [\\"ANY\\"] if unconstrained, [\\"SCALAR\\"] never applies to subject"],
  "tail_types": ["entity types allowed as OBJECT, e.g. Organization; use [\\"SCALAR\\"] if the object is a literal value (number/string/date), [\\"ANY\\"] if unconstrained"],
  "fact_class": "A|B|C",
  "confidence": 0.0-1.0,
  "examples": [{{"subject": "Person1", "object": "Person2"}}]
}}"""

        # Phase 3c: Use centralized LLM retry logic instead of raw httpx call
        result = call_llm_with_retry_sync(
            messages=[{"role": "user", "content": prompt}],
            model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
            user_id="re_embedder",
            timeout=LLMTimeouts.get("ENRICHMENT"),
            operation="ENRICHMENT",
        )

        # call_llm_with_retry_sync() returns PARSED JSON (not raw OpenAI response)
        if not result or not isinstance(result, dict):
            log.warning(f"re_embedder.llm_metadata_query_failed rel_type={candidate_rel} reason=no_valid_response")
            return {}

        # Result is already parsed JSON — validate expected fields
        if not result.get("natural_language"):
            log.warning(f"re_embedder.llm_metadata_query_failed rel_type={candidate_rel} reason=missing_natural_language")
            return {}

        # Extract metadata directly from parsed result
        metadata = result

        def _as_type_list(v):
            # Normalize LLM type field (list | comma-string | single string) → list or None.
            if v is None:
                return None
            if isinstance(v, str):
                parts = [p.strip() for p in v.split(",") if p.strip()]
                return parts or None
            if isinstance(v, (list, tuple)):
                parts = [str(p).strip() for p in v if str(p).strip()]
                return parts or None
            return None

        return {
            "llm_natural_language": metadata.get("natural_language", ""),
            "llm_is_symmetric": metadata.get("is_symmetric", False),
            "llm_inverse_rel_type": metadata.get("inverse_rel_type"),
            "llm_category": metadata.get("category", "other"),
            "llm_head_types": _as_type_list(metadata.get("head_types")),
            "llm_tail_types": _as_type_list(metadata.get("tail_types")),
            "llm_fact_class": metadata.get("fact_class", "B"),
            "llm_confidence": float(metadata.get("confidence", 0.6)),
            "llm_metadata_json": json.dumps(metadata),
        }

    except Exception as e:
        log.warning(f"re_embedder.llm_metadata_query_failed rel_type={candidate_rel} error={type(e).__name__}: {str(e)[:100]}")

    return {}


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
                # dprompt-121: Use embedding cache to avoid re-embedding same types
                candidate_text = f"relationship: {candidate_rel}"
                candidate_vector = _embedding_cache.get(candidate_text)
                if not candidate_vector:
                    candidate_vector = embed_text(
                        candidate_text,
                        qwen_api_url, timeout=10.0, fallback=True
                    )
                    if candidate_vector:
                        _embedding_cache.set(candidate_text, candidate_vector)

                if candidate_vector:
                    # Compute cosine similarity to each existing type
                    best_score = 0.0
                    best_fit = None
                    for ext in existing_types:
                        ext_text = f"relationship: {ext}"
                        # dprompt-121: Check cache first
                        ext_vector = _embedding_cache.get(ext_text)
                        if not ext_vector:
                            ext_vector = embed_text(
                                ext_text,
                                qwen_api_url, timeout=10.0, fallback=True
                            )
                            if ext_vector:
                                _embedding_cache.set(ext_text, ext_vector)

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
                    # dprompt-126: Phase 2 — Query LLM for natural language metadata
                    llm_metadata = _query_llm_for_rel_type_metadata(
                        candidate_rel, subj_type, obj_type, snippet, qwen_api_url
                    )

                    # Register the new rel_type with full metadata
                    label = llm_metadata.get("llm_natural_language", "").split(" is ")[0].title() if llm_metadata.get("llm_natural_language") else candidate_rel.replace('_', ' ').title()
                    natural_language = llm_metadata.get("llm_natural_language", "")
                    is_symmetric = llm_metadata.get("llm_is_symmetric", False)
                    inverse_rel_type = llm_metadata.get("llm_inverse_rel_type")
                    category = llm_metadata.get("llm_category", "other")

                    # Type constraints (Severance #2): prefer the LLM-inferred head/tail types
                    # from the metadata call. Fall back to observed entity types only when the
                    # LLM was uncertain. NEVER persist NULL head/tail types when inference
                    # succeeded — that is the mechanical cause of the 18 empty-head_types rows.
                    head_types = llm_metadata.get("llm_head_types")
                    tail_types = llm_metadata.get("llm_tail_types")
                    is_hierarchy = False

                    if not head_types and subj_type and subj_type != "unknown":
                        head_types = [subj_type]
                    if not tail_types and obj_type and obj_type != "unknown":
                        tail_types = [obj_type]
                    # Last resort so WGM validation is not a silent no-op: unconstrained ANY.
                    if not head_types:
                        head_types = ["ANY"]
                    if not tail_types:
                        tail_types = ["ANY"]

                    # Heuristic: if rel_type suggests classification/taxonomy, mark as hierarchy
                    if any(keyword in candidate_rel.lower() for keyword in ("instance_of", "subclass_of", "member_of", "is_a", "part_of", "type_of")):
                        is_hierarchy = True

                    # dprompt-148: Assign fact_class based on LLM confidence
                    # High confidence (>= 0.7) → Class B (LLM-inferred, can be promoted)
                    # Medium confidence (0.5-0.7) → Class B (staged)
                    # Low confidence (< 0.5) → Class C (ephemeral)
                    llm_confidence = llm_metadata.get("llm_confidence", 0.6)
                    assigned_fact_class = "B" if llm_confidence >= 0.5 else "C"

                    # Severance #2: backfill head/tail types only when currently empty —
                    # never clobber existing good constraints (CASE guards in the UPDATE).
                    cur.execute(
                        "INSERT INTO rel_types"
                        " (rel_type, label, natural_language, engine_generated, confidence, source,"
                        "  head_types, tail_types, is_hierarchy_rel, is_symmetric, inverse_rel_type, category, fact_class)"
                        " VALUES (%s, %s, %s, true, %s, 'llm_evaluated', %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (rel_type) DO UPDATE SET"
                        "  natural_language = EXCLUDED.natural_language,"
                        "  is_symmetric = EXCLUDED.is_symmetric,"
                        "  inverse_rel_type = EXCLUDED.inverse_rel_type,"
                        "  category = EXCLUDED.category,"
                        "  head_types = CASE WHEN (rel_types.head_types IS NULL"
                        "                          OR rel_types.head_types = ARRAY[]::TEXT[])"
                        "                    THEN EXCLUDED.head_types ELSE rel_types.head_types END,"
                        "  tail_types = CASE WHEN (rel_types.tail_types IS NULL"
                        "                          OR rel_types.tail_types = ARRAY[]::TEXT[])"
                        "                    THEN EXCLUDED.tail_types ELSE rel_types.tail_types END,"
                        "  fact_class = EXCLUDED.fact_class",
                        (candidate_rel, label, natural_language, 0.8, head_types, tail_types, is_hierarchy, is_symmetric, inverse_rel_type, category, assigned_fact_class),
                    )
                    stats["approved"] += 1
                    log.info(f"re_embedder.ontology_approved rel_type={candidate_rel} category={category} is_symmetric={is_symmetric} natural_language={natural_language[:50]} {reason}")

                    # Fix B (dprompt-156): propagate new rel_type to entity_taxonomies so
                    # determine_path() can route queries through it immediately.
                    # Category → taxonomy_name mapping is DB-driven: try exact match first,
                    # then ILIKE fallback. No hardcoded category→taxonomy mappings.
                    if category:
                        try:
                            with db_conn.cursor() as _tax_cur:
                                _tax_cur.execute(
                                    """
                                    UPDATE entity_taxonomies
                                    SET rel_types_defining_group = array_append(rel_types_defining_group, %s)
                                    WHERE taxonomy_name = %s
                                      AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])
                                    """,
                                    (candidate_rel, category, candidate_rel),
                                )
                                if _tax_cur.rowcount > 0:
                                    log.info("re_embedder.taxonomy_rel_type_appended",
                                             rel_type=candidate_rel, taxonomy=category)
                                else:
                                    # Exact match found no row — try ILIKE fallback
                                    _tax_cur.execute(
                                        """
                                        UPDATE entity_taxonomies
                                        SET rel_types_defining_group = array_append(rel_types_defining_group, %s)
                                        WHERE taxonomy_name ILIKE %s
                                          AND NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[])
                                        """,
                                        (candidate_rel, category, candidate_rel),
                                    )
                                    if _tax_cur.rowcount > 0:
                                        log.info("re_embedder.taxonomy_rel_type_appended_ilike",
                                                 rel_type=candidate_rel, taxonomy_pattern=category)
                                    else:
                                        log.debug("re_embedder.taxonomy_no_match_for_category",
                                                  rel_type=candidate_rel, category=category)
                        except Exception as _tax_err:
                            log.warning("re_embedder.taxonomy_append_failed",
                                        rel_type=candidate_rel, error=str(_tax_err)[:100])

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

                # Update evaluation record (including LLM metadata if approved)
                if decision == "approved":
                    cur.execute(
                        "UPDATE ontology_evaluations SET"
                        "  re_embedder_decision = %s,"
                        "  re_embedder_confidence = %s,"
                        "  decision_timestamp = now(),"
                        "  decision_reason = %s,"
                        "  created_rel_type = %s,"
                        "  llm_natural_language = %s,"
                        "  llm_is_symmetric = %s,"
                        "  llm_inverse_rel_type = %s,"
                        "  llm_category = %s,"
                        "  llm_fact_class = %s,"
                        "  llm_confidence = %s,"
                        "  llm_metadata_json = %s"
                        " WHERE id = %s",
                        (decision, 0.8, reason, candidate_rel,
                         llm_metadata.get("llm_natural_language", ""),
                         llm_metadata.get("llm_is_symmetric", False),
                         llm_metadata.get("llm_inverse_rel_type"),
                         llm_metadata.get("llm_category", "other"),
                         llm_metadata.get("llm_fact_class", "B"),
                         llm_metadata.get("llm_confidence", 0.6),
                         llm_metadata.get("llm_metadata_json", "{}"),
                         eval_id),
                    )
                else:
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
                         best_score if decision == "mapped" else 0.3,
                         reason, best_fit, best_score,
                         best_fit if decision == "mapped" else None,
                         eval_id),
                    )

            db_conn.commit()

        except Exception as e:
            db_conn.rollback()
            stats["errors"] += 1
            log.error(f"re_embedder.ontology_eval_error eval_id={eval_id} rel_type={candidate_rel}: {e}")

    return stats


def evaluate_correction_signal_candidates(db_conn, qwen_api_url: str) -> dict:
    """
    dprompt-128-P3: Evaluate correction signal candidates from correction_signal_evaluations.
    Runs each poll cycle. Decisions:
      - 'approved': occurrence_count >= 3 → INSERT into correction_signals
      - 'rejected': occurrence_count < 3 → leave as candidate for future evaluation

    Returns: {"approved": int, "rejected": int, "errors": int}
    """
    stats = {"approved": 0, "rejected": 0, "errors": 0}

    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, candidate_pattern, pattern_type,"
                "       first_text_snippet, occurrence_count"
                " FROM correction_signal_evaluations"
                " WHERE re_embedder_decision IS NULL"
                " ORDER BY occurrence_count DESC, last_seen_at DESC"
            )
            candidates = cur.fetchall()
    except Exception as e:
        log.error(f"re_embedder.correction_eval_fetch_failed: {e}")
        return stats

    if not candidates:
        return stats

    log.info(f"re_embedder.correction_eval_candidates count={len(candidates)}")

    for row in candidates:
        eval_id, user_id, candidate_pattern, pattern_type, snippet, occ = row
        try:
            decision = None
            reason = ""

            # ── Decision 1: Pattern frequency ──────────────────────────
            # Threshold: occurrence_count >= 3 means pattern is real and recurring
            if occ >= 3:
                decision = "approved"
                reason = f"occurrence_count={occ} >= 3"

                # Insert into correction_signals table
                with db_conn.cursor() as cur:
                    # Generate label from pattern type
                    label = f"{pattern_type.title()} Pattern"
                    cur.execute("""
                        INSERT INTO correction_signals
                        (pattern, pattern_type, priority, confidence, category, example_usage)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (pattern) DO UPDATE SET
                          occurrence_count = correction_signals.occurrence_count + 1,
                          updated_at = NOW()
                    """, (candidate_pattern, pattern_type, 2, 0.7, user_id, snippet))
                    log.info(f"re_embedder.correction_signal_approved pattern={candidate_pattern[:50]} type={pattern_type}")

            # ── Decision 2: Reject (wait for more occurrences) ──────────
            if not decision:
                decision = "rejected"
                reason = f"occurrence_count={occ} < 3, waiting for more evidence"

            # ── Apply decision ──────────────────────────────────────────
            with db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE correction_signal_evaluations SET
                      re_embedder_decision = %s,
                      re_embedder_confidence = %s
                    WHERE id = %s
                """, (decision, 0.7 if decision == "approved" else 0.3, eval_id))

            stats[decision] += 1
            db_conn.commit()

        except Exception as e:
            db_conn.rollback()
            stats["errors"] += 1
            log.error(f"re_embedder.correction_eval_error eval_id={eval_id} pattern={candidate_pattern[:50]}: {e}")

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


def has_pending_ontology_work(db_conn) -> bool:
    """Check if there are unevaluated ontology candidates (fast query).

    dprompt-121: Event-driven guard to skip evaluation if no pending work.
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ontology_evaluations "
                "WHERE re_embedder_decision IS NULL LIMIT 1"
            )
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        log.warning("re_embedder.pending_ontology_check_failed", error=str(e))
        return False


def has_pending_name_conflicts(db_conn) -> bool:
    """Check if there are unresolved name conflicts (fast query).

    dprompt-121: Event-driven guard to skip conflict resolution if no pending work.
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entity_name_conflicts "
                "WHERE status='pending' LIMIT 1"
            )
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        log.warning("re_embedder.pending_conflicts_check_failed", error=str(e))
        return False


def has_pending_retraction_outcomes(db_conn) -> bool:
    """Check if there are unevaluated retraction outcomes (fast query).

    dprompt-137: Event-driven guard to skip evaluation if no feedback available.
    Returns True if retraction_outcomes table has any rows with was_correct=true or was_correct=false
    (i.e., user has provided feedback/validation).
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM retraction_outcomes "
                "WHERE was_correct IS NOT NULL LIMIT 1"
            )
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        log.warning("re_embedder.pending_retraction_outcomes_check_failed", error=str(e))
        return False


def evaluate_retraction_outcomes(db_conn, frequency_threshold: int = 3) -> dict:
    """Phase 4: Self-building learning loop for retraction signals (dprompt-137).

    Learn from successful/unsuccessful retraction outcomes in real time.
    Auto-register patterns where frequency >= threshold, update metrics for existing patterns.

    Algorithm:
    1. Query retraction_outcomes for rows with was_correct IS NOT NULL (user feedback)
    2. Group by original_message (pattern proxy) to detect frequency
    3. For each high-frequency pattern (freq >= threshold):
       - Check if pattern exists in retraction_signals table
       - If NOT exists: INSERT new pattern with empirical confidence + priority
       - If EXISTS: UPDATE confidence/priority/false_positive_rate based on outcomes
    4. Invalidate Filter cache to force reload on next request

    Returns: {"discovered": int, "updated": int, "errors": int}

    Design rationale:
    - Frequency threshold of 3: prevents single-shot false learning (1-2 occurrences are noise)
    - Confidence = avg(was_correct=true) / total_outcomes (empirical success rate)
    - Priority = success_rate * 100 (0-100 scale), fed back to signal priority ordering
    - False positive rate = (total - correct) / total, used by Filter for semantic gating
    - No hardcoded patterns — all patterns learned from live data
    """
    stats = {"discovered": 0, "updated": 0, "errors": 0}

    try:
        # ────────────────────────────────────────────────────────────────
        # Step 1: Query outcomes grouped by original_message pattern
        # ────────────────────────────────────────────────────────────────
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT original_message,
                       COUNT(*) as freq,
                       CAST(COUNT(CASE WHEN was_correct=true THEN 1 END) AS FLOAT) as correct_count,
                       COUNT(*) as total_count,
                       AVG(detected_confidence) as avg_confidence,
                       AVG(CASE WHEN was_correct=true THEN 1.0 ELSE 0.0 END) as success_rate,
                       retraction_method,
                       MAX(created_at) as last_seen
                FROM retraction_outcomes
                WHERE was_correct IS NOT NULL
                GROUP BY original_message, retraction_method
                HAVING COUNT(*) >= %s
                ORDER BY success_rate DESC, freq DESC
            """, (frequency_threshold,))
            outcomes = cur.fetchall()

        if not outcomes:
            log.debug(f"re_embedder.retraction_outcomes_empty no_feedback_found")
            return stats

        log.info(f"re_embedder.retraction_outcomes_eval found={len(outcomes)} patterns_to_evaluate")

        # ────────────────────────────────────────────────────────────────
        # Step 2: Process each high-frequency pattern
        # ────────────────────────────────────────────────────────────────
        for (pattern, freq, correct_count, total_count, avg_confidence,
             success_rate, retraction_method, last_seen) in outcomes:

            try:
                if not pattern or len(pattern.strip()) < 2:
                    continue  # Skip empty/whitespace patterns

                pattern_lower = pattern.lower().strip()

                # Compute empirical metrics
                false_positive_rate = (1.0 - success_rate) if success_rate is not None else 0.5
                priority = int(success_rate * 100) if success_rate else 50

                # ────────────────────────────────────────────────────────────────
                # Step 2a: Check if pattern already exists in retraction_signals
                # ────────────────────────────────────────────────────────────────
                with db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, signal_category, priority, false_positive_rate "
                        "FROM retraction_signals "
                        "WHERE signal = %s AND language = 'en'",
                        (pattern_lower,)
                    )
                    existing = cur.fetchone()

                if not existing:
                    # ────────────────────────────────────────────────────────────────
                    # Step 2b: NEW PATTERN — insert with empirical confidence
                    # ────────────────────────────────────────────────────────────────
                    category = 'inferred'  # Learned from live data, not seeded
                    if retraction_method == 'semantic':
                        category = 'correction'  # LLM-detected semantic patterns
                    elif retraction_method == 'pattern':
                        category = 'implicit_negation'  # Explicit pattern matches

                    try:
                        with db_conn.cursor() as cur:
                            # Insert into retraction_signals (extraction/detection layer)
                            cur.execute("""
                                INSERT INTO retraction_signals
                                (signal, signal_category, language, priority, false_positive_rate, notes, created_at, updated_at)
                                VALUES (%s, %s, 'en', %s, %s, %s, NOW(), NOW())
                                ON CONFLICT (signal, language) DO NOTHING
                            """, (
                                pattern_lower,
                                category,
                                priority,
                                false_positive_rate,
                                f"Auto-learned: freq={freq}, success_rate={success_rate:.2f}, method={retraction_method}"
                            ))

                            # Also insert into negation_patterns (intent classification layer)
                            # Map retraction_signals to negation_patterns for /classify-intent
                            # Per-schema isolation: no user_id column needed in public schema copy
                            negation_type = 'retraction' if category != 'correction' else 'correction'
                            negation_confidence = min(0.99, priority / 100.0)  # priority 50-100 → confidence 0.5-0.99

                            cur.execute("""
                                INSERT INTO public.negation_patterns
                                (pattern_text, negation_type, learned_from, confidence, created_at)
                                VALUES (%s, %s, 'retraction_outcome_learning', %s, NOW())
                                ON CONFLICT (pattern_text, negation_type) DO UPDATE
                                SET confidence = GREATEST(public.negation_patterns.confidence, %s),
                                    created_at = NOW()
                            """, (
                                pattern_lower,
                                negation_type,
                                negation_confidence,
                                negation_confidence
                            ))

                        db_conn.commit()
                        stats["discovered"] += 1
                        log.info(
                            f"re_embedder.pattern_learned_to_both_tables "
                            f"pattern={pattern[:60]} "
                            f"retraction_signals.priority={priority} "
                            f"negation_patterns.confidence={negation_confidence:.2f} "
                            f"freq={freq} success_rate={success_rate:.2f}"
                        )
                    except Exception as e:
                        db_conn.rollback()
                        stats["errors"] += 1
                        log.error(f"re_embedder.pattern_learning_failed pattern={pattern[:60]}: {e}")

                else:
                    # ────────────────────────────────────────────────────────────────
                    # Step 2c: EXISTING PATTERN — update metrics based on empirical data
                    # ────────────────────────────────────────────────────────────────
                    existing_id, existing_category, existing_priority, existing_fpr = existing

                    # Only update if metrics changed significantly (> 5% delta)
                    priority_delta = abs(priority - existing_priority)
                    fpr_delta = abs(false_positive_rate - existing_fpr)

                    if priority_delta > 5 or fpr_delta > 0.05:
                        try:
                            with db_conn.cursor() as cur:
                                # Update retraction_signals (extraction/detection layer)
                                cur.execute("""
                                    UPDATE retraction_signals SET
                                      priority = %s,
                                      false_positive_rate = %s,
                                      updated_at = NOW(),
                                      notes = %s
                                    WHERE id = %s
                                """, (
                                    priority,
                                    false_positive_rate,
                                    f"Updated: freq={freq}, success_rate={success_rate:.2f}, old_priority={existing_priority}",
                                    existing_id
                                ))

                                # Also update negation_patterns (intent classification layer)
                                # Per-schema isolation: no user_id column needed in public schema copy
                                negation_type = 'retraction' if existing_category != 'correction' else 'correction'
                                negation_confidence = min(0.99, priority / 100.0)

                                cur.execute("""
                                    UPDATE public.negation_patterns SET
                                      confidence = GREATEST(confidence, %s),
                                      created_at = NOW()
                                    WHERE pattern_text = %s AND negation_type = %s
                                """, (
                                    negation_confidence,
                                    pattern_lower,
                                    negation_type
                                ))

                            db_conn.commit()
                            stats["updated"] += 1
                            log.info(
                                f"re_embedder.pattern_updated_in_both_tables "
                                f"pattern={pattern[:60]} "
                                f"retraction_signals.priority={existing_priority}->{priority} "
                                f"negation_patterns.confidence updated "
                                f"fpr={existing_fpr:.2f}->{false_positive_rate:.2f}"
                            )
                        except Exception as e:
                            db_conn.rollback()
                            stats["errors"] += 1
                            log.error(f"re_embedder.retraction_signal_update_failed pattern={pattern[:60]}: {e}")

            except Exception as e:
                stats["errors"] += 1
                log.error(f"re_embedder.retraction_outcome_processing_failed pattern={pattern[:60]}: {e}")

        # ═══════════════════════════════════════════════════════════════════════════════
        # Step 3: Cache Invalidation via TTL (Passive, Not Aggressive)
        # ═══════════════════════════════════════════════════════════════════════════════
        #
        # Filter caches retraction_signals with a 60-second TTL. When re-embedder discovers
        # new patterns and INSERTs them to retraction_signals, Filter's cache remains valid
        # until expiry. Pattern visibility occurs naturally on next /classify-intent call
        # after TTL expiration (within 60 seconds).
        #
        # DESIGN RATIONALE:
        # ─────────────────
        # TTL-based invalidation is optimal because:
        #
        # 1. INDEPENDENCE: Filter and re-embedder run independently. No IPC/shared memory.
        #    TTL is atomic, simpler than distributed cache coherency.
        #
        # 2. GRACEFUL DEGRADATION: If PostgreSQL unavailable, Filter uses stale cache.
        #    Aggressive invalidation would require external coordination.
        #
        # 3. BALANCED TRADE-OFF: 60s window balances freshness (reasonable for learning)
        #    vs DB load (minimal). Pattern discovery is asynchronous (~10s poll cycle).
        #
        # 4. NO THUNDERING HERD: Cache natural expiry spreads reloads over time.
        #    Explicit invalidation could spike DB hits when many patterns discovered.
        #
        # WHY NOT AGGRESSIVE INVALIDATION:
        # ────────────────────────────────
        # ❌ Redis pubsub: Adds Redis dependency, operational complexity
        # ❌ Explicit endpoint: Creates race conditions (invalidation in-flight)
        # ❌ Shared queue: Requires coordinated polling (defeats purpose)
        #
        # TIMELINE EXAMPLE:
        # ─────────────────
        # t=0s    : Filter loads retraction_signals, caches (timestamp=0, TTL=60s)
        # t=35s   : Re-embedder discovers "i'm not X, i'm Y" pattern (frequency=3)
        # t=35s   : INSERT retraction_signals(signal='i''m not', priority=85, ...)
        # t=60s   : Filter cache still valid (35s < 60s TTL)
        # t=62s   : User message triggers /classify-intent
        # t=62s   : Cache check: (62 - 0) = 62s > 60s TTL → EXPIRED
        # t=62s   : Reload from DB → new pattern 'i''m not' visible
        # t=62s+  : New pattern available for classification
        #
        # CODE REFERENCE:
        # ───────────────
        # Filter cache TTL check: openwebui/faultline_function.py lines 776-778
        # ```python
        # if cache_timestamp > 0 and (time() - cache_timestamp) < _RETRACTION_SIGNALS_TTL:
        #     return cached_data  # Cache valid
        # else:
        #     reload_from_db()   # TTL expired, refresh
        # ```
        #
        # DECISION LOG:
        # ─────────────
        # ✅ KEEP TTL-based model (no code changes needed)
        # ❌ Don't implement Redis invalidation (overengineered)
        # ❌ Don't add explicit invalidation endpoint (adds complexity)
        # ✅ Rely on PostgreSQL + TTL for cache coherency
        #
        # Filter will auto-reload signals on next /classify-intent call when cache
        # expires (typically within 60 seconds of pattern discovery).

        if stats["discovered"] > 0 or stats["updated"] > 0:
            log.info(
                f"re_embedder.retraction_learning_cycle_complete "
                f"discovered={stats['discovered']} "
                f"updated={stats['updated']} "
                f"errors={stats['errors']}"
            )

    except Exception as e:
        log.error(f"re_embedder.retraction_outcomes_eval_failed: {e}")
        stats["errors"] += 1

    return stats


def resolve_name_conflicts(db_conn, llm_url: str) -> dict:
    """
    Resolve pending entity name conflicts via LLM context evaluation.

    dprompt-121: When two entities claim the same preferred name, this function
    evaluates the context (facts) of each entity and uses the LLM to decide which
    entity should own the preferred name. Non-destructive: all names preserved,
    only is_preferred flag changes.

    Per-user schema context: entity_name_conflicts and entity_aliases tables
    are per-user. search_path already set by caller. Do NOT add user_id filtering.

    Args:
        db_conn: Database connection (search_path pre-set to user's schema)
        llm_url: LLM endpoint URL (e.g., http://localhost:11434/v1/chat/completions)

    Returns:
        dict with stats: {"resolved": int, "errors": int, "skipped": int}
    """
    stats = {"resolved": 0, "errors": 0, "skipped": 0}

    try:
        # ────────────────────────────────────────────────────────────────
        # Step 1: Query pending conflicts (limit to avoid overload)
        # ────────────────────────────────────────────────────────────────
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.entity_id_a, COALESCE(a1.alias, c.entity_id_a),
                       c.entity_id_b, COALESCE(a2.alias, c.entity_id_b), c.alias
                FROM entity_name_conflicts c
                LEFT JOIN entity_aliases a1 ON a1.entity_id = c.entity_id_a AND a1.is_preferred = true
                LEFT JOIN entity_aliases a2 ON a2.entity_id = c.entity_id_b AND a2.is_preferred = true
                WHERE c.status = 'pending'
                ORDER BY c.created_at ASC
                LIMIT 20
            """)
            conflicts = cur.fetchall()

        if not conflicts:
            log.debug("re_embedder.name_conflicts_none_pending")
            return stats

        log.info(f"re_embedder.name_conflict_resolution_start pending={len(conflicts)}")

        # ────────────────────────────────────────────────────────────────
        # Step 2: Process each conflict
        # ────────────────────────────────────────────────────────────────
        for conflict_id, entity_id_1, entity_name_1, entity_id_2, entity_name_2, disputed_name in conflicts:
            try:
                # ────────────────────────────────────────────────────────────────
                # Build context for Entity 1
                # ────────────────────────────────────────────────────────────────
                context_1 = ""
                with db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*),
                               STRING_AGG(DISTINCT rel_type, ', ' ORDER BY rel_type) as rel_types
                        FROM facts
                        WHERE subject_id = %s OR object_id = %s
                    """, (entity_id_1, entity_id_1))
                    row = cur.fetchone()
                    if row:
                        fact_count, rel_types = row
                        rel_types_str = rel_types or "(none)"
                        context_1 = (
                            f"Entity 1 ('{entity_name_1}', UUID: {entity_id_1[:8]}...) "
                            f"has {fact_count} facts with relationship types: {rel_types_str}"
                        )
                    else:
                        context_1 = f"Entity 1 ('{entity_name_1}') has no facts."

                # ────────────────────────────────────────────────────────────────
                # Build context for Entity 2
                # ────────────────────────────────────────────────────────────────
                context_2 = ""
                with db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*),
                               STRING_AGG(DISTINCT rel_type, ', ' ORDER BY rel_type) as rel_types
                        FROM facts
                        WHERE subject_id = %s OR object_id = %s
                    """, (entity_id_2, entity_id_2))
                    row = cur.fetchone()
                    if row:
                        fact_count, rel_types = row
                        rel_types_str = rel_types or "(none)"
                        context_2 = (
                            f"Entity 2 ('{entity_name_2}', UUID: {entity_id_2[:8]}...) "
                            f"has {fact_count} facts with relationship types: {rel_types_str}"
                        )
                    else:
                        context_2 = f"Entity 2 ('{entity_name_2}') has no facts."

                # ────────────────────────────────────────────────────────────────
                # Call LLM to disambiguate (fail-loud on LLM errors)
                # ────────────────────────────────────────────────────────────────
                from src.api.llm_client import build_llm_payload, get_llm_headers

                prompt = (
                    f"Two entities claim the name '{disputed_name}':\n\n"
                    f"{context_1}\n\n"
                    f"{context_2}\n\n"
                    f"Which entity should have '{disputed_name}' as its preferred display name? "
                    f"Answer with ONLY 'Entity 1' or 'Entity 2' (no explanation)."
                )

                messages = [{"role": "user", "content": prompt}]
                payload = build_llm_payload(
                    messages=messages,
                    model=os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                    user_id="re_embedder",
                    temperature=0.2,
                    max_tokens=10
                )

                # Use global HTTP client with timeout
                try:
                    response = _http_client_sync.post(
                        llm_url,
                        json=payload,
                        headers=get_llm_headers("re_embedder"),
                        timeout=10.0
                    )
                    response.raise_for_status()
                except Exception as e:
                    log.error(
                        f"re_embedder.name_conflict_llm_call_failed "
                        f"conflict_id={conflict_id} "
                        f"error={str(e)}"
                    )
                    stats["errors"] += 1
                    continue

                # ────────────────────────────────────────────────────────────────
                # Parse LLM decision
                # ────────────────────────────────────────────────────────────────
                try:
                    result = response.json()
                    # Handle both direct JSON and wrapped response
                    if isinstance(result, dict) and "choices" in result:
                        llm_choice = result["choices"][0]["message"]["content"].strip().lower()
                    elif isinstance(result, dict) and "content" in result:
                        llm_choice = result.get("content", "").strip().lower()
                    else:
                        llm_choice = str(result).strip().lower()

                    # Determine winner
                    if "entity 1" in llm_choice:
                        winner_id = entity_id_1
                        loser_id = entity_id_2
                        winner_name = entity_name_1
                        loser_name = entity_name_2
                    elif "entity 2" in llm_choice:
                        winner_id = entity_id_2
                        loser_id = entity_id_1
                        winner_name = entity_name_2
                        loser_name = entity_name_1
                    else:
                        log.warning(
                            f"re_embedder.name_conflict_llm_ambiguous "
                            f"conflict_id={conflict_id} "
                            f"llm_response={llm_choice[:100]}"
                        )
                        stats["skipped"] += 1
                        continue

                except Exception as e:
                    log.error(
                        f"re_embedder.name_conflict_parse_failed "
                        f"conflict_id={conflict_id} "
                        f"error={str(e)}"
                    )
                    stats["errors"] += 1
                    continue

                # ────────────────────────────────────────────────────────────────
                # Update aliases based on LLM decision
                # ────────────────────────────────────────────────────────────────
                try:
                    with db_conn.cursor() as cur:
                        # Winner: set preferred
                        cur.execute(
                            "UPDATE entity_aliases SET is_preferred = true "
                            "WHERE entity_id = %s AND alias = %s",
                            (winner_id, disputed_name)
                        )

                        # Loser: unset preferred
                        cur.execute(
                            "UPDATE entity_aliases SET is_preferred = false "
                            "WHERE entity_id = %s AND alias = %s",
                            (loser_id, disputed_name)
                        )

                        # If loser has no other aliases, create fallback
                        cur.execute(
                            "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = %s",
                            (loser_id,)
                        )
                        loser_alias_count = cur.fetchone()[0]

                        if loser_alias_count == 1:
                            # Loser's only alias is the disputed name; create fallback
                            fallback_alias = f"{disputed_name}_entity_{loser_id[:8]}"
                            cur.execute(
                                "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                                "VALUES (%s, %s, false) "
                                "ON CONFLICT (entity_id, alias) DO NOTHING",
                                (loser_id, fallback_alias)
                            )

                        # Mark conflict as resolved
                        cur.execute(
                            "UPDATE entity_name_conflicts SET "
                            "status = 'resolved', resolved_by = %s, resolved_at = NOW() "
                            "WHERE id = %s",
                            (f"Winner: {winner_name}; Loser fallback: {fallback_alias if loser_alias_count == 1 else 'existing'}",
                             conflict_id)
                        )

                    db_conn.commit()
                    stats["resolved"] += 1

                    log.info(
                        f"re_embedder.name_conflict_resolved "
                        f"conflict_id={conflict_id} "
                        f"disputed_name={disputed_name} "
                        f"winner={winner_name} "
                        f"loser={loser_name}"
                    )

                except Exception as e:
                    db_conn.rollback()
                    log.error(
                        f"re_embedder.name_conflict_update_failed "
                        f"conflict_id={conflict_id} "
                        f"error={str(e)}"
                    )
                    stats["errors"] += 1
                    continue

            except Exception as e:
                # Error isolation: don't crash re-embedder if one conflict fails
                log.error(
                    f"re_embedder.name_conflict_processing_failed "
                    f"conflict_id={conflict_id} "
                    f"error={str(e)}"
                )
                stats["errors"] += 1

        if stats["resolved"] > 0 or stats["errors"] > 0:
            log.info(
                f"re_embedder.name_conflict_resolution_complete "
                f"resolved={stats['resolved']} "
                f"errors={stats['errors']} "
                f"skipped={stats['skipped']}"
            )

    except Exception as e:
        log.error(f"re_embedder.name_conflict_resolution_failed (non-fatal): {e}")
        stats["errors"] += 1

    return stats


def detect_embedding_model_change() -> None:
    """Auto-detect if embedding model version changed; clear cache if so.

    dprompt-121: On startup, compare model version to stored version.
    If mismatch, clear embedding cache (v1.5→v2.0 embeddings incomparable).
    """
    if not _embedding_cache.client:
        return

    embedding_model_version = os.getenv("EMBEDDING_MODEL_VERSION", "nomic-v1.5")
    try:
        stored_version = _embedding_cache.client.get("_embedding_model_version")
        if stored_version and stored_version != embedding_model_version:
            log.warning(
                "embedding_cache.model_version_changed",
                old=stored_version,
                new=embedding_model_version,
            )
            deleted = _embedding_cache.clear_pattern(f"{_embedding_cache.prefix}*")
            log.info("embedding_cache.cleared_model_change", entries_deleted=deleted)

        # Store current model version
        _embedding_cache.client.set("_embedding_model_version", embedding_model_version)
    except Exception as e:
        log.warning("embedding_cache.model_detection_failed", error=str(e))


def _get_redis_client(redis_url: str = None) -> Optional[redis.Redis]:
    """Get or initialize Redis client for queue operations.

    Args:
        redis_url: Optional explicit Redis URL (auto-detected if not provided)

    Returns:
        Redis client or None if connection fails
    """
    try:
        url = redis_url or os.getenv("REDIS_URL") or _detect_redis_endpoint()
        client = redis.from_url(url, decode_responses=True, socket_timeout=5)
        client.ping()
        return client
    except Exception as e:
        log.warning(f"redis_client_initialization_failed: {e}")
        return None


def consume_reembedder_queue(db_conn, redis_client: redis.Redis, qwen_api_url: str) -> int:
    """
    Consume events from Redis queue and process them.
    Processes high-priority class_c queue first, then per-user queues.
    Non-blocking: if queue empty, returns immediately.

    Args:
        db_conn: PostgreSQL connection
        redis_client: Redis client
        qwen_api_url: LLM endpoint URL

    Returns:
        Number of events processed
    """
    if not redis_client:
        return 0

    processed = 0

    try:
        # Step 1: Check high-priority class_c_ingest queue (blocking pop with timeout)
        try:
            event_json = redis_client.blpop("faultline:queue:class_c", timeout=1)
            if event_json:
                event_data = event_json[1]  # blpop returns (key, value)
                if process_reembedder_event(db_conn, redis_client, qwen_api_url, event_data):
                    processed += 1
                # Continue to next iteration to check for more events
                return processed + consume_reembedder_queue(db_conn, redis_client, qwen_api_url)
        except Exception as e:
            log.debug(f"queue_consumer.class_c_pop_error: {e}")

        # Step 2: Check a few per-user queues (non-blocking)
        # In production, this would enumerate active users, here we try a few
        try:
            # Use KEYS to find active user queues (limited scan)
            cursor = 0
            for _ in range(5):  # Check up to 5 queues per iteration
                cursor, keys = redis_client.scan(cursor, match="faultline:queue:*", count=10)

                for key in keys:
                    if key == "faultline:queue:class_c":
                        continue  # Already handled above

                    try:
                        event_json = redis_client.lpop(key)
                        if event_json:
                            if process_reembedder_event(db_conn, redis_client, qwen_api_url, event_json):
                                processed += 1
                    except Exception as e:
                        log.debug(f"queue_consumer.user_queue_error key={key}: {e}")

                if cursor == 0:
                    break  # Scan complete
        except Exception as e:
            log.debug(f"queue_consumer.scan_error: {e}")

    except Exception as e:
        log.error(f"queue_consumer.error: {e}")

    return processed


def process_reembedder_event(
    db_conn,
    redis_client: redis.Redis,
    qwen_api_url: str,
    event_json: str
) -> bool:
    """
    Process a single re-embedder event from Redis.

    CRITICAL: Validates user_id before any operation.

    Args:
        db_conn: PostgreSQL connection
        redis_client: Redis client
        qwen_api_url: LLM endpoint URL
        event_json: JSON string from Redis

    Returns:
        True if processed successfully, False otherwise
    """
    try:
        event = json.loads(event_json)
    except Exception as e:
        log.error(f"reembedder_event.json_parse_error: {e}")
        return False

    event_type = event.get("event_type")
    user_id = event.get("user_id")

    # CRITICAL: Validate user_id before any DB operation
    if not user_id or not isinstance(user_id, str) or len(user_id) < 4:
        log.error(f"reembedder_event.invalid_user_id event_type={event_type} user_id_len={len(user_id or '')}")
        return False

    try:
        if event_type == "class_c_ingest":
            rel_type = event.get("rel_type", "").lower().strip()
            confidence = event.get("confidence", 0.4)

            if rel_type and len(rel_type) > 0:
                # Evaluate novel rel_type for ontology learning
                with db_conn.cursor() as cur:
                    # Check if rel_type is already known
                    cur.execute(
                        "SELECT rel_type FROM rel_types WHERE rel_type = %s LIMIT 1",
                        (rel_type,)
                    )
                    if not cur.fetchone():
                        # Novel rel_type — log for re_embedder evaluation
                        log.debug(f"reembedder_event_processed event_type=class_c_ingest "
                                 f"rel_type={rel_type} confidence={confidence} user_id={user_id[:8]}")
                        return True
            return True

        elif event_type == "negation_pattern_novel":
            pattern_hash = event.get("pattern_hash")
            confidence = event.get("confidence", 0.4)

            if pattern_hash:
                # Learn negation pattern by hash
                learned = learn_negation_pattern_by_hash(db_conn, user_id, pattern_hash, confidence)
                if learned:
                    log.debug(f"reembedder_event_processed event_type=negation_pattern_novel "
                             f"pattern_hash={pattern_hash} confidence={confidence} user_id={user_id[:8]}")
                return True

        elif event_type == "correction_feedback":
            confidence_bin = event.get("confidence_bin")
            feedback_type = event.get("feedback_type", "correction")

            if confidence_bin:
                # Record correction feedback and adjust gate
                recorded = record_confidence_feedback(db_conn, user_id, confidence_bin, feedback_type)
                if recorded:
                    adjusted = adjust_confidence_gate(db_conn, user_id)
                    log.debug(f"reembedder_event_processed event_type=correction_feedback "
                             f"confidence_bin={confidence_bin} feedback_type={feedback_type} "
                             f"gate_adjusted={adjusted} user_id={user_id[:8]}")
                return True

        else:
            log.warning(f"reembedder_event.unknown_type event_type={event_type} user_id={user_id[:8]}")
            return False

    except Exception as e:
        log.error(f"reembedder_event_failed event_type={event_type} "
                 f"user_id_prefix={user_id[:8] if user_id else 'none'} error={str(e)}")
        return False


def learn_negation_pattern_by_hash(
    db_conn,
    user_id: str,
    pattern_hash: str,
    confidence: float = 0.4
) -> bool:
    """
    Learn a negation pattern based on hash match.
    Pattern hash is used to identify patterns without storing raw text in Redis.
    This function logs the pattern learning for re-embedder evaluation.

    Args:
        db_conn: PostgreSQL connection
        user_id: User UUID
        pattern_hash: SHA256[:16] hash of pattern (from Redis event)
        confidence: Starting confidence (typically 0.4)

    Returns:
        True if pattern was learned/updated, False otherwise
    """
    try:
        with db_conn.cursor() as cur:
            # Query for existing pattern by hash (if pattern_hash column exists in DB)
            # Otherwise, this is logged for future evaluation by re_embedder

            # For now, just track the hash as a generic pattern
            # In future, this would match against actual pattern_text via hash comparison
            # Per-user schema: no user_id filter needed — schema provides isolation
            cur.execute(
                """
                SELECT id FROM negation_patterns
                WHERE pattern_hash IS NOT NULL
                  AND pattern_hash = %s
                LIMIT 1
                """,
                (pattern_hash,)
            )

            existing = cur.fetchone()
            if existing:
                # Pattern already exists — increment confirmed count
                cur.execute(
                    """
                    UPDATE negation_patterns
                    SET confirmed_count = confirmed_count + 1,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (existing[0],)
                )
                log.debug(f"negation_pattern_confirmed user_id={user_id[:8]} pattern_hash={pattern_hash}")
            else:
                # New pattern — log it as a candidate for future learning
                # The pattern_text field will be populated by the re-embedder when it
                # reconstructs the full pattern from context (if available)
                # Per-user schema: no user_id column needed
                cur.execute(
                    """
                    INSERT INTO negation_patterns
                    (pattern_text, pattern_hash, negation_type, confidence, confirmed_count, learned_from)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (pattern_text, negation_type) DO UPDATE
                    SET confirmed_count = negation_patterns.confirmed_count + 1,
                        pattern_hash = COALESCE(EXCLUDED.pattern_hash, negation_patterns.pattern_hash),
                        updated_at = now()
                    """,
                    (f"hash_{pattern_hash}", pattern_hash, "retraction", confidence, 1, "re_embedder_inferred")
                )
                log.debug(f"negation_pattern_learned user_id={user_id[:8]} pattern_hash={pattern_hash}")

        db_conn.commit()
        return True

    except Exception as e:
        log.error(f"learn_negation_pattern_error user_id={user_id[:8]} pattern_hash={pattern_hash}: {e}")
        db_conn.rollback()
        return False


def record_confidence_feedback(
    db_conn,
    user_id: str,
    confidence_bin: str,
    feedback_type: str = "correction"
) -> bool:
    """
    Record confidence feedback for gate adjustment.
    Updates intent_confidence_feedback table.

    Args:
        db_conn: PostgreSQL connection
        user_id: User UUID
        confidence_bin: Bin like "0.65-0.75"
        feedback_type: "correction" or "confirmation"

    Returns:
        True if recorded, False on error
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO intent_confidence_feedback
                (user_id, confidence_bin, feedback_type, count)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (user_id, confidence_bin, feedback_type)
                DO UPDATE SET count = intent_confidence_feedback.count + 1,
                              created_at = now()
                """,
                (user_id, confidence_bin, feedback_type)
            )
        db_conn.commit()
        return True

    except Exception as e:
        log.error(f"record_confidence_feedback_error user_id={user_id[:8]} "
                 f"confidence_bin={confidence_bin}: {e}")
        db_conn.rollback()
        return False


def adjust_confidence_gate(db_conn, user_id: str) -> bool:
    """
    Adjust per-user confidence gate threshold based on correction feedback.

    Algorithm:
    - If 0.65-0.75 band has >30% corrections → lower gate to 0.65
    - If <0.60 band has <10% corrections → raise gate to 0.75
    - Otherwise keep at 0.70 (default)

    Args:
        db_conn: PostgreSQL connection
        user_id: User UUID

    Returns:
        True if gate was adjusted, False otherwise
    """
    try:
        with db_conn.cursor() as cur:
            # Query feedback history
            cur.execute(
                """
                SELECT confidence_bin, feedback_type, SUM(count) as total
                FROM intent_confidence_feedback
                WHERE user_id = %s
                GROUP BY confidence_bin, feedback_type
                """,
                (user_id,)
            )

            feedback = cur.fetchall()
            gate = 0.70  # Default
            adjusted = False

            # Parse feedback into bins
            bins = {}
            for row in feedback:
                conf_bin, fb_type, count = row
                if conf_bin not in bins:
                    bins[conf_bin] = {"correction": 0, "confirmation": 0}
                bins[conf_bin][fb_type] = count

            # Apply adjustment rules
            if "0.65-0.75" in bins:
                total = bins["0.65-0.75"].get("correction", 0) + bins["0.65-0.75"].get("confirmation", 0)
                corrections = bins["0.65-0.75"].get("correction", 0)
                if total > 0 and corrections / total > 0.30:
                    gate = 0.65
                    adjusted = True

            if "0.50-0.60" in bins:
                total = bins["0.50-0.60"].get("correction", 0) + bins["0.50-0.60"].get("confirmation", 0)
                corrections = bins["0.50-0.60"].get("correction", 0)
                if total > 0 and corrections / total < 0.10:
                    gate = 0.75
                    adjusted = True

            # Store adjusted gate threshold (in memory cache during session)
            # For persistence across sessions, store in a confidence_gates table
            cur.execute(
                """
                INSERT INTO confidence_gates (user_id, threshold, adjusted_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                SET threshold = %s, adjusted_at = now()
                """,
                (user_id, gate, gate)
            )

        db_conn.commit()
        if adjusted:
            log.info(f"confidence_gate_adjusted user_id={user_id[:8]} new_threshold={gate}")
        return adjusted

    except Exception as e:
        log.error(f"adjust_confidence_gate_error user_id={user_id[:8]}: {e}")
        db_conn.rollback()
        return False


def evaluate_extraction_patterns(db_conn) -> dict:
    """
    Job 6: Evaluate extraction pattern accuracy and bootstrap confidence scores.

    Steps:
    1. Query extraction_pattern_matches for user feedback (confirmed/rejected)
    2. Calculate accuracy for each pattern: confirmed / (confirmed + rejected)
    3. Archive underperforming patterns (accuracy < 0.3)
    4. Promote high-confidence patterns (confirmed_count >= 3)
    5. Update global_confidence scores in extraction_patterns table
    6. Log all decisions for monitoring

    Returns: {
        "evaluated": int,
        "archived": int,
        "promoted": int,
        "confidence_updates": int,
        "errors": int
    }
    """
    stats = {
        "evaluated": 0,
        "archived": 0,
        "promoted": 0,
        "confidence_updates": 0,
        "errors": 0,
    }

    try:
        with db_conn.cursor() as cur:
            # Query patterns with feedback data
            cur.execute("""
                SELECT
                    ep.id,
                    ep.pattern_regex,
                    ep.rel_type,
                    COALESCE(ep.confirmed_count, 0) as confirmed_count,
                    COALESCE(ep.rejected_count, 0) as rejected_count,
                    COALESCE(ep.correction_count, 0) as correction_count,
                    ep.global_confidence,
                    ep.frequency
                FROM extraction_patterns ep
                WHERE ep.is_active = true
                ORDER BY ep.frequency DESC, ep.global_confidence DESC
            """)
            patterns = cur.fetchall()

    except Exception as e:
        log.error(f"re_embedder.extraction_pattern_fetch_failed: {e}")
        stats["errors"] += 1
        return stats

    if not patterns:
        log.info("re_embedder.extraction_pattern_eval no_patterns_to_evaluate")
        return stats

    log.info(f"re_embedder.extraction_pattern_eval_start count={len(patterns)}")

    for pattern_row in patterns:
        pattern_id, pattern_regex, rel_type, confirmed, rejected, corrections, confidence, frequency = pattern_row
        stats["evaluated"] += 1

        try:
            # Calculate accuracy
            total_feedback = confirmed + rejected
            accuracy = confirmed / total_feedback if total_feedback > 0 else 0.0

            # Decision 1: Archive underperforming patterns
            if accuracy < 0.3 and total_feedback >= 3:
                with db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE extraction_patterns
                        SET is_active = false, archived_at = NOW()
                        WHERE id = %s
                    """, (pattern_id,))
                stats["archived"] += 1
                log.info(
                    f"re_embedder.extraction_pattern_archived "
                    f"pattern_id={pattern_id} rel_type={rel_type} "
                    f"accuracy={accuracy:.2f} confirmed={confirmed} rejected={rejected}"
                )
                continue

            # Decision 2: Update confidence based on accuracy
            new_confidence = confidence
            if accuracy >= 0.85:
                new_confidence = 0.90
            elif accuracy >= 0.70:
                new_confidence = 0.80
            elif accuracy < 0.50 and total_feedback >= 3:
                new_confidence = 0.50

            if new_confidence != confidence:
                with db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE extraction_patterns
                        SET global_confidence = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (new_confidence, pattern_id))
                stats["confidence_updates"] += 1
                log.info(
                    f"re_embedder.extraction_pattern_confidence_updated "
                    f"pattern_id={pattern_id} rel_type={rel_type} "
                    f"old={confidence:.2f} new={new_confidence:.2f}"
                )

            # Decision 3: Promote new patterns after sufficient confirmation
            # Metadata-driven: only boost engine_generated (LLM-discovered) rel_types,
            # not system-defined ones (which already have validated Class A/B assignment)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT engine_generated FROM rel_types WHERE rel_type = %s LIMIT 1",
                    (rel_type,)
                )
                rt_row = cur.fetchone()
            is_novel_rel_type = rt_row is None or rt_row[0]  # Novel if missing or engine_generated=true

            if confirmed >= 3 and is_novel_rel_type:
                # Novel patterns (not in original hardcoded list)
                with db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE extraction_patterns
                        SET global_confidence = 0.80, updated_at = NOW()
                        WHERE id = %s AND global_confidence < 0.80
                    """, (pattern_id,))
                    if cur.rowcount > 0:
                        stats["promoted"] += 1
                        log.info(
                            f"re_embedder.extraction_pattern_promoted "
                            f"pattern_id={pattern_id} rel_type={rel_type} "
                            f"confirmed={confirmed}"
                        )

        except Exception as e:
            log.error(
                f"re_embedder.extraction_pattern_eval_error "
                f"pattern_id={pattern_id}: {e}"
            )
            stats["errors"] += 1

    # Commit all changes
    try:
        db_conn.commit()
        log.info(
            f"re_embedder.extraction_pattern_eval_complete "
            f"evaluated={stats['evaluated']} "
            f"archived={stats['archived']} "
            f"promoted={stats['promoted']} "
            f"confidence_updates={stats['confidence_updates']} "
            f"errors={stats['errors']}"
        )
    except Exception as e:
        log.error(f"re_embedder.extraction_pattern_eval_commit_failed: {e}")
        db_conn.rollback()
        stats["errors"] += 1

    return stats


def main():
    """Main poll loop."""
    global _http_client_sync

    # Log startup environment for debugging container issues (using extra= for structured data)
    log.info("re_embedder.startup_environment", extra={
        "has_postgres_dsn": bool(os.getenv("POSTGRES_DSN")),
        "has_qdrant_url": bool(os.getenv("QDRANT_URL")),
        "has_redis_url": bool(os.getenv("REDIS_URL")),
        "reembed_interval": os.getenv("REEMBED_INTERVAL", "60"),
        "pythonpath": os.getenv("PYTHONPATH", "not_set")
    })

    postgres_dsn = os.getenv("POSTGRES_DSN")
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    from src.api.llm_client import get_backend_endpoint, get_endpoint_list as _get_llm_endpoint_list
    _typed_endpoint = get_backend_endpoint()
    if _typed_endpoint:
        qwen_api_url = _typed_endpoint
    else:
        _endpoints = _get_llm_endpoint_list()
        qwen_api_url = _endpoints[0] if _endpoints else "http://localhost:11434/v1/chat/completions"
    interval = int(os.getenv("REEMBED_INTERVAL", "60"))  # dprompt-121: Changed from 10 to 60
    confidence_threshold = float(os.getenv("QDRANT_SYNC_CONFIDENCE_THRESHOLD", "0.0"))

    if not postgres_dsn:
        log.error("POSTGRES_DSN not configured - re_embedder cannot start")
        log.error("re_embedder.startup_failed reason=missing_postgres_dsn")
        return

    # Initialize persistent HTTP client for pooled connections
    _http_client_sync = httpx.Client(timeout=httpx.Timeout(30.0), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    log.info(f"re_embedder.http_client_initialized")

    # Register LLM HTTP client cleanup so it drains on process exit (SIGTERM or KeyboardInterrupt)
    atexit.register(close_llm_http_client)

    # dprompt-121: Detect if embedding model changed (auto-clear cache if so)
    detect_embedding_model_change()

    log.info(f"re_embedder.start interval={interval}s qdrant_url={qdrant_url} confidence_threshold={confidence_threshold} loglevel=INFO")
    log.info("re_embedder.entering_main_loop")

    # Initialize Redis client for event queue
    redis_url = os.getenv("REDIS_URL")
    redis_client = _get_redis_client(redis_url)
    if redis_client:
        log.info("re_embedder.redis_client_initialized")
    else:
        log.warning("re_embedder.redis_client_unavailable queue_events_disabled")

    while True:
        try:
            # Phase 3c: Check circuit breaker health for awareness
            breaker_status = _get_circuit_breaker_status()
            if breaker_status["is_open"]:
                log.warning("re_embedder.circuit_breaker_open skipping_llm_work_this_cycle")

            # At the top of every iteration, before any DB query, ensure the default
            # collection exists. This recovers a deleted collection within one loop
            # cycle regardless of whether there are any unsynced rows.
            default_collection = os.getenv("QDRANT_COLLECTION", "faultline-test")
            ensure_collection(default_collection, qdrant_url)

            # PHASE 2: Get list of all ready user schemas to process independently
            with psycopg2.connect(postgres_dsn) as admin_db:
                with admin_db.cursor() as cur:
                    cur.execute("""
                        SELECT user_id, schema_name FROM public.user_provisioning
                        WHERE status = 'ready'
                        ORDER BY ready_at ASC
                    """)
                    ready_schemas = [(row[0], row[1]) for row in cur.fetchall()]

            if ready_schemas:
                log.info(f"re_embedder.ready_schemas_found count={len(ready_schemas)}")

            # PHASE 2b: Process each user schema independently
            for user_id, schema_name in ready_schemas:
                try:
                    with psycopg2.connect(postgres_dsn) as db_per_user:
                        with db_per_user.cursor() as cur:
                            cur.execute(f"SET search_path TO {schema_name}, public")
                        db_per_user.commit()

                        # Promote staged facts for this user
                        n_promoted = promote_staged_facts(db_per_user, qdrant_url, user_id=user_id, schema_name=schema_name)
                        if n_promoted:
                            log.info(f"re_embedder.promotion_complete user_id={user_id[:8]} schema={schema_name} promoted={n_promoted}")

                        # Expire stale Class C facts for this user
                        n_expired = expire_staged_facts(db_per_user, qdrant_url, user_id=user_id)
                        if n_expired:
                            log.info(f"re_embedder.expiry_complete user_id={user_id[:8]} expired={n_expired}")

                        # JOB 2 — Promote Class C rows that earned hit_count >= 3 (query-scoped
                        # hits) to Class B. Classify-if-needed (default B-2). Run BEFORE the
                        # hit-decay sweep so a row that just reached threshold promotes instead
                        # of being decremented in the same cycle.
                        n_c_promoted = promote_class_c_hits(
                            db_per_user, qdrant_url, qwen_api_url,
                            user_id=user_id, schema_name=schema_name
                        )
                        if n_c_promoted:
                            log.info(f"re_embedder.class_c_promotion_complete user_id={user_id[:8]} promoted={n_c_promoted}")

                        # JOB 1 — Decay Class C query-hit counter for idle rows (30d window
                        # elapsed with no hit): hit_count -= 1, reset window, DROP at <= 0.
                        c_decay = decay_class_c_hits(db_per_user, qdrant_url, user_id=user_id)
                        if c_decay["decremented"] or c_decay["dropped"]:
                            log.info(
                                f"re_embedder.class_c_decay user_id={user_id[:8]} "
                                f"decremented={c_decay['decremented']} dropped={c_decay['dropped']}"
                            )

                        # Fetch and embed unsynced facts for this user
                        rows = fetch_unsynced(db_per_user, user_id, confidence_threshold)
                        if rows:
                            log.info(f"re_embedder.batch_start user_id={user_id[:8]} count={len(rows)}")
                            collection = derive_collection(user_id)
                            ensure_collection(collection, qdrant_url)

                            # Resolve display names for batch
                            rows = resolve_display_names_for_facts(db_per_user, rows)

                            for row in rows:
                                try:
                                    text = f"{row['subject_display']} {row['rel_type']} {row['object_display']}"
                                    vector = embed_text(text, qwen_api_url)
                                    if upsert_to_qdrant(row, vector, collection, qdrant_url):
                                        mark_synced(db_per_user, row["id"])
                                        log.info(f"re_embedder.synced fact_id={row['id']} user_id={user_id[:8]}")
                                except Exception as e:
                                    log.error(f"re_embedder.row_error fact_id={row['id']} user_id={user_id[:8]}: {e}")
                                    continue

                        # Fetch and embed unsynced staged facts for this user
                        staged_rows = fetch_unsynced_staged(db_per_user, user_id)
                        if staged_rows:
                            log.info(f"re_embedder.staged_batch user_id={user_id[:8]} count={len(staged_rows)}")
                            collection = derive_collection(user_id)
                            ensure_collection(collection, qdrant_url)

                            staged_rows = resolve_display_names_for_facts(db_per_user, staged_rows)
                            for row in staged_rows:
                                try:
                                    text = f"{row['subject_display']} {row['rel_type']} {row['object_display']}"
                                    vector = embed_text(text, qwen_api_url)
                                    if upsert_to_qdrant(row, vector, collection, qdrant_url):
                                        with db_per_user.cursor() as cur:
                                            cur.execute(
                                                "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                                (row["staged_id"],)
                                            )
                                        db_per_user.commit()
                                        log.info(f"re_embedder.staged_synced staged_id={row['staged_id']} user_id={user_id[:8]}")
                                except Exception as e:
                                    log.error(f"re_embedder.staged_row_error staged_id={row['staged_id']} user_id={user_id[:8]}: {e}")

                except Exception as e:
                    log.error(f"re_embedder.per_user_promotion_failed user_id={user_id[:8] if user_id else 'unknown'} schema={schema_name}: {e}")

            # GROWTH ENGINE WIRE #2: Adjust per-user confidence gates based on feedback
            # Phase 2c: Intent classification gate self-healing (runs every cycle)
            # Enables system to learn from intent classification patterns without hardcoded thresholds
            try:
                with psycopg2.connect(postgres_dsn) as db:
                    with db.cursor() as cur:
                        # Find users with significant feedback history (>= 10 classifications)
                        cur.execute("""
                            SELECT user_id
                            FROM intent_confidence_feedback
                            GROUP BY user_id
                            HAVING SUM(count) >= 10
                            ORDER BY user_id
                        """)
                        users_to_adjust = [row[0] for row in cur.fetchall()]

                    if users_to_adjust:
                        adjusted_count = 0
                        for user_id in users_to_adjust:
                            try:
                                # Query feedback distribution
                                with db.cursor() as cur:
                                    cur.execute("""
                                        SELECT confidence_bin, feedback_type, count
                                        FROM intent_confidence_feedback
                                        WHERE user_id = %s
                                        ORDER BY confidence_bin ASC
                                    """, (user_id,))
                                    feedback_rows = cur.fetchall()

                                if not feedback_rows:
                                    continue

                                # Compute optimal gate threshold based on feedback distribution
                                # Strategy: find confidence level where corrections spike (wrong classifications)
                                # Lower threshold where there are many corrections; raise where false positives
                                total_feedback = sum(row[2] for row in feedback_rows)
                                corrections = sum(row[2] for row in feedback_rows if row[1] == 'correction')
                                correction_rate = corrections / total_feedback if total_feedback > 0 else 0.0

                                # Aggressive gate: if no corrections, assume default 0.70 is fine
                                # Only learn stricter gates from actual user corrections
                                if corrections == 0:
                                    recommended_gate = 0.50  # Aggressive: allow more through
                                    log.debug(f"re_embedder.gate_aggressive user_id={user_id[:8]} reason=no_corrections")
                                else:
                                    # Find the confidence bin with highest correction density
                                    # (indicates GLiNER2 is unreliable in that range)
                                    bin_correction_rates = {}
                                    for bin_range, feedback_type, count in feedback_rows:
                                        if bin_range not in bin_correction_rates:
                                            bin_correction_rates[bin_range] = {'corrections': 0, 'total': 0}
                                        bin_correction_rates[bin_range]['total'] += count
                                        if feedback_type == 'correction':
                                            bin_correction_rates[bin_range]['corrections'] += count

                                    # Recommended gate: lowest bin where correction rate < 15%
                                    recommended_gate = 0.70  # Default
                                    for bin_range in sorted(bin_correction_rates.keys(), reverse=True):
                                        stats = bin_correction_rates[bin_range]
                                        if stats['total'] >= 5:  # Need statistical significance
                                            bin_correction_rate = stats['corrections'] / stats['total']
                                            if bin_correction_rate < 0.15:  # Threshold: >85% accuracy
                                                # Extract lower bound of this bin as gate
                                                bin_start = float(bin_range.split('-')[0])
                                                recommended_gate = bin_start
                                                break

                                # Persist recommended gate to confidence_gates (what /classify-intent reads)
                                with db.cursor() as cur:
                                    cur.execute("""
                                        INSERT INTO confidence_gates (user_id, threshold, adjusted_at)
                                        VALUES (%s, %s, now())
                                        ON CONFLICT (user_id) DO UPDATE
                                        SET threshold = %s, adjusted_at = now()
                                    """, (user_id, recommended_gate, recommended_gate))
                                db.commit()
                                adjusted_count += 1
                                log.info(f"re_embedder.gate_adjusted user_id={user_id[:8]} recommended_gate={recommended_gate:.2f} correction_rate={correction_rate:.2%}")

                            except Exception as e:
                                log.warning(f"re_embedder.gate_adjustment_failed user_id={user_id[:8]}: {e}")

                        if adjusted_count > 0:
                            log.info(f"re_embedder.gate_adjustment_cycle adjusted={adjusted_count} users")

            except Exception as e:
                log.warning(f"re_embedder.gate_adjustment_phase_error (non-blocking): {e}")

            # GROWTH ENGINE JOB 7: Promote Learned Patterns
            # Issue #2: When LLM fallback learns a pattern and it's confirmed 3+ times,
            # promote its confidence to 0.95 (high confidence). This enables faster
            # pattern matching in future /classify-intent calls.
            try:
                with psycopg2.connect(postgres_dsn) as db:
                    with db.cursor() as cur:
                        # Find all user schemas
                        cur.execute("""
                            SELECT schema_name FROM public.user_provisioning
                            WHERE status = 'ready'
                        """)
                        schemas = [row[0] for row in cur.fetchall()]

                    for schema_name in schemas:
                        try:
                            with db.cursor() as cur:
                                # Promote low-confidence learned patterns when confirmed 3+ times
                                cur.execute(f"""
                                    UPDATE {schema_name}.negation_patterns
                                    SET confidence = 0.95, updated_at = NOW()
                                    WHERE learned_from = 'LLM_FALLBACK'
                                    AND confirmed_count >= 3
                                    AND confidence < 0.95
                                    AND pattern_text IS NOT NULL
                                """)
                                promoted = cur.rowcount
                                if promoted > 0:
                                    db.commit()
                                    log.info(f"re_embedder.job7_patterns_promoted schema={schema_name} count={promoted}")
                        except Exception as e:
                            log.debug(f"re_embedder.job7_schema_error schema={schema_name}: {str(e)[:100]}")
            except Exception as e:
                log.warning(f"re_embedder.job7_pattern_promotion_error (non-blocking): {e}")

            # Continue with default schema for global work (embeddings, ontology, etc)
            with psycopg2.connect(postgres_dsn) as db:
                # Phase 3: Process Redis queue events first (high-priority)
                # Non-blocking: if Redis unavailable, system continues with DB poll
                if redis_client:
                    try:
                        queue_events_processed = consume_reembedder_queue(db, redis_client, qwen_api_url)
                        if queue_events_processed > 0:
                            log.info(f"re_embedder.queue_events_processed count={queue_events_processed}")
                    except Exception as e:
                        log.warning(f"re_embedder.queue_consumer_error (non-blocking): {e}")
                        # Continue to DB poll even if queue fails

                # NOTE: Per-user promotion and fact syncing now happens in PHASE 2b
                # (see per-schema isolation above in the ready_schemas loop)

                # dprompt-121: Event-driven ontology evaluation (skip if no pending work)
                # Phase 3c: Wrap in error isolation to prevent background loop crash
                try:
                    if has_pending_ontology_work(db):
                        ontology_stats = evaluate_ontology_candidates(db, qwen_api_url)
                        if any(v > 0 for v in ontology_stats.values()):
                            log.info(
                                f"re_embedder.ontology_eval "
                                f"approved={ontology_stats['approved']} "
                                f"mapped={ontology_stats['mapped']} "
                                f"rejected={ontology_stats['rejected']} "
                                f"errors={ontology_stats['errors']}"
                            )
                        # Phase 3 (Severance #3): newly-approved rel_types live in the DB but are
                        # invisible to the backend uvicorn process (separate OS process) until it
                        # reloads _REL_TYPE_META. Trigger the cross-process refresh endpoint.
                        if ontology_stats.get("approved", 0) > 0 or ontology_stats.get("mapped", 0) > 0:
                            try:
                                _r = httpx.post(
                                    "http://faultline:8000/internal/refresh-intent-pattern-caches",
                                    timeout=5.0,
                                )
                                if _r.status_code == 200:
                                    log.info("re_embedder.ontology_approval_cache_refresh_triggered")
                                else:
                                    log.warning(f"re_embedder.ontology_approval_cache_refresh_failed status={_r.status_code}")
                            except Exception as _re:
                                log.warning(f"re_embedder.ontology_approval_cache_refresh_error: {_re}")
                    else:
                        log.debug("re_embedder.no_pending_ontology_work")
                except Exception as e:
                    log.error(f"re_embedder.ontology_eval_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if ontology eval fails

                # Retroactive head_types/tail_types sweep (Severance #2, Phase 2): self-heal the
                # existing rel_types rows that have NULL/empty head_types (32% of the ontology,
                # including instance_of/subclass_of/member_of). Bounded batch per cycle (LIMIT 10).
                # Uses the SAME LLM metadata call so type constraints come from inference, not guesswork.
                try:
                    with db.cursor() as cur:
                        cur.execute(
                            "SELECT rel_type, head_types, tail_types, natural_language"
                            " FROM rel_types"
                            " WHERE (head_types IS NULL OR head_types = ARRAY[]::TEXT[]"
                            "        OR tail_types IS NULL OR tail_types = ARRAY[]::TEXT[])"
                            " ORDER BY confidence DESC NULLS LAST"
                            " LIMIT 10"
                        )
                        _sweep_rows = cur.fetchall()

                    _sweep_updated = 0
                    for _rt, _ht, _tt, _nl in _sweep_rows:
                        try:
                            _md = _query_llm_for_rel_type_metadata(
                                _rt, "unknown", "unknown", _nl or "", qwen_api_url
                            )
                            _new_head = _md.get("llm_head_types")
                            _new_tail = _md.get("llm_tail_types")
                            # Only fill what is missing; do not overwrite existing non-empty values.
                            _set_head = (not _ht) and bool(_new_head)
                            _set_tail = (not _tt) and bool(_new_tail)
                            if not (_set_head or _set_tail):
                                continue
                            with db.cursor() as cur:
                                cur.execute(
                                    "UPDATE rel_types SET"
                                    "  head_types = CASE WHEN (head_types IS NULL OR head_types = ARRAY[]::TEXT[])"
                                    "                    THEN %s ELSE head_types END,"
                                    "  tail_types = CASE WHEN (tail_types IS NULL OR tail_types = ARRAY[]::TEXT[])"
                                    "                    THEN %s ELSE tail_types END"
                                    " WHERE rel_type = %s",
                                    (_new_head if _set_head else None,
                                     _new_tail if _set_tail else None,
                                     _rt),
                                )
                            db.commit()
                            _sweep_updated += 1
                            log.info(f"re_embedder.head_tail_sweep_filled rel_type={_rt} "
                                     f"head_types={_new_head if _set_head else _ht} "
                                     f"tail_types={_new_tail if _set_tail else _tt}")
                        except Exception as _sw_err:
                            try:
                                db.rollback()
                            except Exception:
                                pass
                            log.warning(f"re_embedder.head_tail_sweep_row_failed rel_type={_rt}: {_sw_err}")

                    if _sweep_updated > 0:
                        # Propagate updated type constraints to the backend uvicorn process (Phase 3).
                        try:
                            _r = httpx.post(
                                "http://faultline:8000/internal/refresh-intent-pattern-caches",
                                timeout=5.0,
                            )
                            if _r.status_code == 200:
                                log.info(f"re_embedder.head_tail_sweep_cache_refreshed updated={_sweep_updated}")
                        except Exception as _re:
                            log.warning(f"re_embedder.head_tail_sweep_cache_refresh_failed: {_re}")
                except Exception as e:
                    log.warning(f"re_embedder.head_tail_sweep_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")

                # dprompt-065: Async taxonomy discovery for novel rel_types (deferred from ingest)
                # Runs in poll loop — no blocking LLM call in ingest hot path
                # Phase 3c: Wrap entire subsystem in error isolation to prevent crash
                try:
                    from src.api.main import _llm_discover_taxonomy_from_facts, _load_taxonomy_cache
                    from src.api.llm_client import build_llm_payload

                    with db.cursor() as cur:
                        cur.execute(
                            "SELECT DISTINCT rel_type FROM staged_facts "
                            "WHERE rel_type NOT IN (SELECT rel_type FROM rel_types) LIMIT 10"
                        )
                        novel_rels = [row[0] for row in cur.fetchall()]

                    if novel_rels:
                        for rel_type in novel_rels:
                            try:
                                discovered = _llm_discover_taxonomy_from_facts(
                                    db, "system", [{"rel_type": rel_type}]
                                )
                                if discovered and discovered.get("taxonomy_name"):
                                    with db.cursor() as cur:
                                        cur.execute(
                                            "INSERT INTO entity_taxonomies "
                                            "(taxonomy_name, description, member_entity_types, "
                                            "rel_types_defining_group, has_transitivity, "
                                            "transitive_rel_types, is_hierarchical, parent_rel_type, source) "
                                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                                            "ON CONFLICT (taxonomy_name) DO NOTHING",
                                            (
                                                discovered.get("taxonomy_name"),
                                                discovered.get("description", ""),
                                                discovered.get("member_entity_types", "{}"),
                                                discovered.get("rel_types_defining_group", []),
                                                discovered.get("has_transitivity", False),
                                                discovered.get("transitive_rel_types", "{}"),
                                                discovered.get("is_hierarchical", False),
                                                discovered.get("parent_rel_type"),
                                                "engine_learned_re_embedder",
                                            ),
                                        )
                                    db.commit()
                                    _load_taxonomy_cache(db)
                                    log.info("re_embedder.taxonomy_discovered_async",
                                            rel_type=rel_type,
                                            taxonomy=discovered.get("taxonomy_name"))
                            except Exception as e:
                                log.warning("re_embedder.taxonomy_discovery_failed",
                                           rel_type=rel_type, error=str(e))
                except Exception as e:
                    log.error(f"re_embedder.taxonomy_discovery_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if taxonomy discovery fails

                # dprompt-128-P3: Evaluate correction signal candidates
                # Patterns that occur >= 3 times are auto-approved to correction_signals
                # Phase 3c: Wrap in error isolation to prevent background loop crash
                try:
                    correction_stats = evaluate_correction_signal_candidates(db, qwen_api_url)
                    if any(v > 0 for v in correction_stats.values()):
                        log.info(
                            f"re_embedder.correction_eval "
                            f"approved={correction_stats['approved']} "
                            f"rejected={correction_stats['rejected']} "
                            f"errors={correction_stats['errors']}"
                        )
                except Exception as e:
                    log.error(f"re_embedder.correction_signal_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if correction eval fails

                # dprompt-137: Evaluate retraction outcomes for continuous learning
                # Auto-register high-frequency patterns, update metrics for existing patterns
                # Phase 3c: Wrap in error isolation to prevent background loop crash
                try:
                    if has_pending_retraction_outcomes(db):
                        retraction_stats = evaluate_retraction_outcomes(db, frequency_threshold=3)
                        if any(v > 0 for v in [retraction_stats["discovered"], retraction_stats["updated"]]):
                            log.info(
                                f"re_embedder.retraction_learning_complete "
                                f"discovered={retraction_stats['discovered']} "
                                f"updated={retraction_stats['updated']} "
                                f"errors={retraction_stats['errors']}"
                            )
                    else:
                        log.debug("re_embedder.no_pending_retraction_outcomes")
                except Exception as e:
                    log.error(f"re_embedder.retraction_outcomes_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if retraction outcomes eval fails

                # dprompt-121: Resolve name conflicts via LLM context evaluation
                # Event-driven: only run if there are pending conflicts
                # Phase 3c: Wrap in error isolation to prevent background loop crash
                try:
                    if has_pending_name_conflicts(db):
                        conflict_stats = resolve_name_conflicts(db, qwen_api_url)
                        if conflict_stats["resolved"] > 0:
                            log.info(
                                f"re_embedder.name_conflicts_resolved "
                                f"resolved={conflict_stats['resolved']} "
                                f"errors={conflict_stats['errors']} "
                                f"skipped={conflict_stats['skipped']}"
                            )
                    else:
                        log.debug("re_embedder.no_pending_name_conflicts")
                except Exception as e:
                    log.error(f"re_embedder.name_conflict_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if name conflict resolution fails

                # Job 6: Evaluate extraction patterns for accuracy and bootstrap confidence
                # Scoring phase: analyze user feedback on extraction patterns, update confidence scores
                # Phase 3c: Wrap in error isolation to prevent background loop crash
                try:
                    pattern_stats = evaluate_extraction_patterns(db)
                    if any(v > 0 for v in pattern_stats.values()):
                        log.info(
                            f"re_embedder.extraction_pattern_eval "
                            f"evaluated={pattern_stats['evaluated']} "
                            f"archived={pattern_stats['archived']} "
                            f"promoted={pattern_stats['promoted']} "
                            f"confidence_updates={pattern_stats['confidence_updates']} "
                            f"errors={pattern_stats['errors']}"
                        )
                        # Signal backend to reload pattern caches (critical: ensures updates propagate)
                        try:
                            refresh_resp = httpx.post(
                                f"http://faultline:8000/internal/refresh-intent-pattern-caches",
                                timeout=5.0
                            )
                            if refresh_resp.status_code == 200:
                                log.info("re_embedder.pattern_cache_refresh_triggered")
                            else:
                                log.warning(f"re_embedder.pattern_cache_refresh_failed status={refresh_resp.status_code}")
                        except Exception as refresh_err:
                            log.warning(f"re_embedder.pattern_cache_refresh_error: {refresh_err}")
                    else:
                        log.debug("re_embedder.no_pending_extraction_pattern_work")
                except Exception as e:
                    log.error(f"re_embedder.extraction_pattern_subsystem_error (non-fatal): {type(e).__name__}: {str(e)[:200]}")
                    # Continue with next subsystem even if pattern evaluation fails

                # dprompt-153: Evict stale intent_pattern_cache entries
                # TTL-based: delete expired rows with confirmed_count < 3; grace-extend the rest
                try:
                    with db.cursor() as cur:
                        cur.execute("""
                            DELETE FROM public.intent_pattern_cache
                            WHERE is_permanent = false
                              AND expires_at < now()
                              AND confirmed_count < 3
                        """)
                        deleted = cur.rowcount
                        cur.execute("""
                            UPDATE public.intent_pattern_cache
                            SET expires_at = now() + INTERVAL '7 days'
                            WHERE is_permanent = false
                              AND expires_at IS NOT NULL
                              AND expires_at < now()
                              AND confirmed_count >= 3
                        """)
                        extended = cur.rowcount
                        db.commit()
                    if deleted > 0 or extended > 0:
                        log.info("re_embedder.pattern_cache_eviction", deleted=deleted, extended=extended)
                except Exception as e:
                    log.warning(f"re_embedder.pattern_cache_eviction_failed (non-fatal): {type(e).__name__}: {str(e)[:100]}")

                # Job 7: Fill in missing natural_language for rel_types in use.
                # Finds rel_types with NULL natural_language that appear in recent facts,
                # calls LLM to generate the template, stores it. Runs at most 5 per cycle
                # to avoid LLM saturation. Self-limiting: once filled, never runs again
                # for that rel_type.
                try:
                    with db.cursor() as cur:
                        # Find rel_types in active use that lack natural_language.
                        # Prioritise those seen in recent facts but any will do.
                        cur.execute(
                            """SELECT rel_type FROM rel_types
                               WHERE (natural_language IS NULL OR natural_language = '')
                               ORDER BY confidence DESC
                               LIMIT 5"""
                        )
                        missing_nl = [row[0] for row in cur.fetchall()]

                    for rt in missing_nl:
                        try:
                            messages = [
                                {"role": "system", "content":
                                 "You are an ontology expert. Respond with ONLY a JSON object, no markdown."},
                                {"role": "user", "content":
                                 f'Generate a short human-readable phrase for the relationship type "{rt}".\n'
                                 f'IMPORTANT: Use X for the subject and Y for the object in your phrase.\n'
                                 f'Example: "parent_of" → "X is the parent of Y", "has_ip" → "X has IP address Y", "spouse" → "X and Y are spouses"\n'
                                 f'Respond with ONLY: {{"natural_language": "your phrase here"}}'},
                            ]
                            result = call_llm_with_retry_sync(
                                messages=messages,
                                model=os.environ.get("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                                user_id="re_embedder",
                                timeout=10,
                                operation="natural_language_fill",
                            )
                            nl = result.get("natural_language", "").strip() if result else ""
                            # CLAUDE.md constraint: natural_language templates MUST contain X placeholder.
                            # Reject and log any LLM-generated value missing X — never store broken templates.
                            if nl and 'X' not in nl:
                                log.warning(
                                    "re_embedder.natural_language_missing_placeholder",
                                    rel_type=rt,
                                    generated_value=nl,
                                    reason="LLM response missing X subject placeholder — rejecting, will retry next cycle"
                                )
                                nl = ""  # Force retry on next poll cycle — do not commit broken state
                            if nl:
                                with db.cursor() as cur:
                                    cur.execute(
                                        "UPDATE rel_types SET natural_language = %s"
                                        " WHERE rel_type = %s AND (natural_language IS NULL OR natural_language = '')",
                                        (nl, rt),
                                    )
                                db.commit()
                                log.info(f"re_embedder.natural_language_filled rel_type={rt} value={nl!r}")
                        except Exception as nl_err:
                            log.warning(f"re_embedder.natural_language_fill_failed rel_type={rt}: {nl_err}")
                except Exception as e:
                    log.warning(f"re_embedder.natural_language_job_error (non-fatal): {e}")

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

        except Exception as e:
            log.error(f"re_embedder.loop_error: {e}")

        time.sleep(interval)


def extract_retraction_pattern(text: str, rel_type: str, action: str, user_id: str,
                               llm_url: str, db_conn) -> dict:
    """
    Extract reusable negation pattern from a user retraction.

    Args:
        text: user's retraction message
        rel_type: relationship type being retracted (if known)
        action: "delete", "correct", "negate", "supersede"
        user_id: user UUID
        llm_url: LLM endpoint URL
        db_conn: database connection for context

    Returns:
        dict with: pattern_text, pattern_type, negation_type, confidence
        or None if extraction failed
    """
    try:
        from src.api.llm_client import build_llm_payload, get_llm_headers
        from src.api.llm_calls import call_llm_with_retry_sync, LLMTimeouts

        messages = [
            {
                "role": "system",
                "content": f"{_FAULTLINE_INTERNAL_PREFIX} You are a pattern learner. Extract reusable negation patterns from retractions. Respond only with valid JSON, no markdown."
            },
            {
                "role": "user",
                "content": f"""Extract the negation pattern from this user retraction:

Text: {text}
Relationship: {rel_type if rel_type else 'unknown'}
Action: {action}

Respond with ONLY this JSON structure:
{{
  "pattern_text": "normalized_pattern_string",
  "pattern_type": "substring|semantic",
  "negation_type": "deletion|negation|correction|general",
  "confidence": 0.0 to 1.0
}}

Rules:
- pattern_text: lowercase, underscores for spaces, no special chars, 4-50 chars
- pattern_type: "substring" for explicit markers like "forget about", "semantic" for complex patterns
- negation_type: "deletion" (remove fact), "negation" (fact is false), "correction" (replace with new), "general" (unclear)
- confidence: 0.90+ for clear patterns, 0.70-0.89 for probable, <0.70 for uncertain

Respond with ONLY the JSON, no explanation."""
            }
        ]

        result = call_llm_with_retry_sync(
            messages=messages,
            model=os.getenv("CATEGORY_LLM_MODEL", "qwen2.5-coder"),
            user_id=user_id,
            timeout=LLMTimeouts.get("ENRICHMENT", 30),
            operation="pattern_extraction",
        )

        # Validate required fields
        required = ["pattern_text", "pattern_type", "negation_type", "confidence"]
        for field in required:
            if field not in result:
                log.warning("re_embedder.pattern_extraction_missing_field",
                           field=field, rel_type=rel_type)
                return None

        # Normalize pattern_text
        pattern_text = result.get("pattern_text", "").lower()
        pattern_text = re.sub(r'[^a-z0-9_]', '_', pattern_text)  # Only alphanumerics and underscores
        pattern_text = re.sub(r'_+', '_', pattern_text)  # Remove duplicate underscores
        pattern_text = pattern_text.strip('_')  # Strip leading/trailing underscores

        if not pattern_text or len(pattern_text) < 3:
            log.warning("re_embedder.pattern_text_invalid_after_normalization",
                       original=result.get("pattern_text"))
            return None

        result["pattern_text"] = pattern_text
        return result

    except Exception as e:
        log.warning("re_embedder.pattern_extraction_failed",
                   rel_type=rel_type, error=str(e))
        return None


def store_retraction_pattern(pattern_text: str, pattern_type: str, negation_type: str,
                             confidence: float, db_conn) -> bool:
    """
    Store learned retraction pattern in negation_patterns table.
    Per-user schema: patterns are isolated by schema, not by user_id column.

    Args:
        pattern_text: normalized pattern string
        pattern_type: "substring" or "semantic" (mapped to learned_from)
        negation_type: "deletion", "negation", "correction", or "general"
        confidence: confidence score (0.0-1.0)
        db_conn: database connection

    Returns:
        True if stored successfully, False otherwise
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """INSERT INTO negation_patterns
                   (pattern_text, negation_type, learned_from, confidence, confirmed_count)
                   VALUES (%s, %s, %s, %s, 1)
                   ON CONFLICT (pattern_text, negation_type) DO UPDATE SET
                       confirmed_count = negation_patterns.confirmed_count + 1,
                       confidence = GREATEST(negation_patterns.confidence, EXCLUDED.confidence)
                """,
                (pattern_text, negation_type, pattern_type, confidence),
            )

        db_conn.commit()
        log.info("re_embedder.pattern_stored",
                pattern=pattern_text, negation_type=negation_type,
                confidence=confidence)
        return True

    except Exception as e:
        log.error("re_embedder.pattern_storage_failed",
                 pattern=pattern_text, error=str(e))
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


if __name__ == "__main__":
    main()