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


def fetch_unsynced(db_conn) -> list[dict]:
    """Fetch all facts where qdrant_synced = false."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject_id, object_id, rel_type, provenance, user_id,
                   confidence, confirmed_count, last_seen_at, contradicted_by
            FROM facts
            WHERE qdrant_synced = false
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
            "user_id": row[5] if row[5] else "anonymous",
            "confidence": row[6] if row[6] is not None else 1.0,
            "confirmed_count": row[7] if row[7] is not None else 0,
            "last_seen_at": row[8],
            "contradicted_by": row[9],
        }
        for row in rows
    ]


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
                            "subject": row["subject_id"],
                            "object": row["object_id"],
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


def main():
    """Main poll loop."""
    postgres_dsn = os.getenv("POSTGRES_DSN")
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    qwen_api_url = os.getenv("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
    interval = int(os.getenv("REEMBED_INTERVAL", "10"))

    if not postgres_dsn:
        log.error("POSTGRES_DSN not configured")
        return

    log.info(f"re_embedder.start interval={interval}s qdrant_url={qdrant_url}")

    while True:
        try:
            # At the top of every iteration, before any DB query, ensure the default
            # collection exists. This recovers a deleted collection within one loop
            # cycle regardless of whether there are any unsynced rows.
            default_collection = os.getenv("QDRANT_COLLECTION", "faultline-test")
            ensure_collection(default_collection, qdrant_url)

            db = psycopg2.connect(postgres_dsn)
            try:
                rows = fetch_unsynced(db)

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

                for row in rows:
                    try:
                        text = f"{row['subject_id']} {row['rel_type']} {row['object_id']}"
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

            finally:
                db.close()

        except Exception as e:
            log.error(f"re_embedder.loop_error: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
