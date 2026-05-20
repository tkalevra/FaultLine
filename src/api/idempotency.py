"""Idempotency manager for deduplicating retried requests.

Prevents silent request duplication when LLM calls are interrupted mid-stream.
Each request gets a unique idempotency_key; duplicates return cached response.
"""

import json
import hashlib
import structlog
from typing import Optional, Any
import redis
import os

log = structlog.get_logger()


class IdempotencyManager:
    """Cache responses by idempotency_key to detect and handle duplicate requests."""

    def __init__(self, redis_url: Optional[str] = None):
        """Initialize Redis connection for idempotency cache.

        Args:
            redis_url: Redis connection URL (defaults to env REDIS_URL)
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.ttl = 3600  # Cache responses for 1 hour

        try:
            self.client = redis.from_url(self.redis_url, decode_responses=True)
            self.client.ping()
            log.info("idempotency.redis_connected", url=self.redis_url)
        except Exception as e:
            log.warning("idempotency.redis_connection_failed", error=str(e))
            self.client = None

    def generate_key(self, text: str, user_id: str, endpoint: str) -> str:
        """Generate idempotency key from request content + user + endpoint.

        Uses SHA256 hash of concatenated fields for compact key.
        Same request from same user on same endpoint = same key.
        """
        content = f"{text}|{user_id}|{endpoint}".encode('utf-8')
        return hashlib.sha256(content).hexdigest()

    def get_cached_response(self, idempotency_key: str) -> Optional[dict]:
        """Retrieve cached response for this idempotency key.

        Returns:
            Cached response dict if found, None otherwise.
        """
        if not self.client:
            return None

        try:
            cached = self.client.get(f"idempotent:{idempotency_key}")
            if cached:
                log.info("idempotency.cache_hit", key=idempotency_key[:12])
                return json.loads(cached)
        except Exception as e:
            log.warning("idempotency.cache_get_error", error=str(e))

        return None

    def cache_response(self, idempotency_key: str, response: dict) -> bool:
        """Cache response for this idempotency key.

        Returns:
            True if cached successfully, False otherwise.
        """
        if not self.client:
            return False

        try:
            # Convert Pydantic models to dicts for JSON serialization
            # IngestResponse contains FactResult objects which need .model_dump()
            if isinstance(response, dict) and "facts" in response:
                response = dict(response)
                if isinstance(response["facts"], list):
                    response["facts"] = [
                        f.model_dump() if hasattr(f, "model_dump") else f
                        for f in response["facts"]
                    ]

            self.client.setex(
                f"idempotent:{idempotency_key}",
                self.ttl,
                json.dumps(response)
            )
            log.info("idempotency.cache_set", key=idempotency_key[:12], ttl=self.ttl)
            return True
        except Exception as e:
            log.warning("idempotency.cache_set_error", error=str(e))
            return False

    def is_duplicate(self, idempotency_key: str) -> bool:
        """Check if this idempotency key has been seen before."""
        return self.get_cached_response(idempotency_key) is not None
