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

# Cache of known-good Qdrant collections to avoid redundant checks
_known_collections: set = set()


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
            SELECT id, subject_id, object_id, rel_type, provenance, user_id
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
            "user_id": row[5] if len(row) > 5 and row[5] else "anonymous",
        }
        for row in rows
    ]


def embed_text(text: str, qwen_api_url: str) -> list[float]:
    """
    Embed text using Qwen API embeddings endpoint.
    Falls back to deterministic hash vector on failure.
    """
    embed_url = qwen_api_url.replace("/chat/completions", "/embeddings")

    try:
        response = httpx.post(
            embed_url,
            json={
                "model": "text-embedding-nomic-embed-text-v1.5",
                "input": text
            },
            timeout=30.0
        )
        response.raise_for_status()
        data = response.json()

        # Extract embedding from response
        if "data" in data and len(data["data"]) > 0:
            return data["data"][0]["embedding"]

        raise ValueError("Invalid embedding response format")

    except Exception as e:
        log.warning(f"re_embedder.embed_failed text_preview={text[:50]} falling back to hash vector: {e}")
        return hash_vector(text)


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


def ensure_collection_exists(collection: str, qdrant_url: str) -> None:
    """
    Check if Qdrant collection exists, create if not.
    Uses local cache to avoid redundant checks.
    """
    if collection in _known_collections:
        return

    try:
        # Check if collection exists
        response = httpx.get(
            f"{qdrant_url}/collections/{collection}",
            timeout=10.0
        )

        if response.status_code == 200:
            _known_collections.add(collection)
            return

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
            create_response.raise_for_status()
            _known_collections.add(collection)
            log.info(f"re_embedder.collection_created collection={collection}")
            return

        raise Exception(f"Unexpected status checking collection: {response.status_code}")

    except Exception as e:
        log.error(f"re_embedder.collection_error collection={collection}: {e}")
        raise


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
            db = psycopg2.connect(postgres_dsn)
            try:
                rows = fetch_unsynced(db)

                if rows:
                    log.info(f"re_embedder.batch_start count={len(rows)}")

                for row in rows:
                    try:
                        # Embed the fact
                        text = f"{row['subject_id']} {row['rel_type']} {row['object_id']}"
                        vector = embed_text(text, qwen_api_url)

                        # Determine collection
                        collection = derive_collection(row["user_id"])

                        # Ensure collection exists
                        ensure_collection_exists(collection, qdrant_url)

                        # Upsert to Qdrant
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
