"""Idempotency manager for deduplicating retried requests.

Prevents silent request duplication when LLM calls are interrupted mid-stream.
Each request gets a unique idempotency_key; duplicates return cached response.
"""

import json
import hashlib
import structlog
from typing import Optional, Any, Tuple
import redis
import os
import uuid
import time

log = structlog.get_logger()

# Global Redis URL — set via environment variable or use Docker default
_REDIS_URL = os.getenv("REDIS_URL", "redis://faultline-redis:6379/0")


class IdempotencyManager:
    """Cache responses by idempotency_key to detect and handle duplicate requests."""

    def __init__(self, redis_url: Optional[str] = None):
        """Initialize Redis connection for idempotency cache.

        Args:
            redis_url: Redis connection URL (defaults to global _REDIS_URL)
        """
        self.redis_url = redis_url or _REDIS_URL
        self.ttl = 3600  # Cache responses for 1 hour

        try:
            self.client = redis.from_url(self.redis_url, decode_responses=True)
            self.client.ping()
            log.info("idempotency.redis_connected", url=self.redis_url)
        except Exception as e:
            log.warning("idempotency.redis_connection_failed", error=str(e))
            self.client = None

    def generate_key(self, text: str, user_id: str, endpoint: str,
                     messages: list = None, typed_entities: dict = None,
                     memory_facts: list = None, is_correction: bool = False) -> str:
        """Generate idempotency key from ALL request parameters (request fingerprinting).

        Includes text, user, endpoint, conversation context, entity hints, memory, correction flag.
        Two requests with same text but different context get different keys (no collisions).
        """
        msg_hash = hashlib.sha256(json.dumps(messages or []).encode()).hexdigest()[:8]
        entity_hash = hashlib.sha256(json.dumps(typed_entities or {}).encode()).hexdigest()[:8]
        memory_hash = hashlib.sha256(json.dumps(memory_facts or []).encode()).hexdigest()[:8]
        correction_flag = str(is_correction)

        content = f"{text}|{user_id}|{endpoint}|{msg_hash}|{entity_hash}|{memory_hash}|{correction_flag}".encode('utf-8')
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

    def cache_response(self, idempotency_key: str, response: dict, ttl_seconds: int = None) -> bool:
        """Cache response for this idempotency key.

        Validates response before caching — don't cache errors or incomplete responses.
        Returns:
            True if cached successfully, False otherwise.
        """
        if not self.client:
            return False

        # Response validation: only cache successful, complete responses
        if response.get("status") == "failed":
            log.warning("idempotency.not_caching_failed_response", key=idempotency_key[:12])
            return False

        if response.get("status") == "error":
            log.warning("idempotency.not_caching_error_response", key=idempotency_key[:12])
            return False

        # For streaming responses: ensure we have actual content
        if "edges" in response and not response["edges"]:
            log.warning("idempotency.not_caching_empty_response", key=idempotency_key[:12])
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

            ttl = ttl_seconds or self.ttl
            self.client.setex(
                f"idempotent:{idempotency_key}",
                ttl,
                json.dumps(response)
            )
            log.info("idempotency.response_cached", key=idempotency_key[:12], ttl=ttl)
            return True
        except Exception as e:
            log.warning("idempotency.cache_set_error", error=str(e))
            return False

    def get_or_lock(self, idempotency_key: str, lock_ttl: int = 30) -> Tuple[bool, Optional[dict]]:
        """Attempt to get cached response or acquire lock for processing.

        Prevents race conditions where concurrent identical requests both process the LLM.
        Uses Redis SET NX EX for atomic lock + expiry (avoids SETNX/EXPIRE race).

        Returns:
            (acquired_lock: bool, cached_response: Optional[dict])
            - (False, response_dict): Cache hit, return immediately
            - (True, None): Lock acquired, proceed with processing
            - (False, None): Lock held by another request, waited and timed out
        """
        if not self.client:
            return (True, None)  # Fallback: proceed without lock if Redis unavailable

        # Try to get cached response first
        cached = self.client.get(f"idempotent:{idempotency_key}")
        if cached:
            log.info("idempotency.lock.cache_hit", key=idempotency_key[:12])
            return (False, json.loads(cached))

        # Try to acquire lock (atomic: SET key value NX EX ttl)
        lock_key = f"lock:{idempotency_key}"
        lock_value = str(uuid.uuid4())  # Unique lock owner ID (safe release)

        try:
            acquired = self.client.set(
                lock_key,
                lock_value,
                nx=True,  # Only if not exists
                ex=lock_ttl  # Atomic expiry (avoids EXPIRE race condition)
            )

            if acquired:
                log.info("idempotency.lock.acquired", key=idempotency_key[:12])
                # Store lock_value in instance for safe release later
                if not hasattr(self, '_active_locks'):
                    self._active_locks = {}
                self._active_locks[idempotency_key] = lock_value
                return (True, None)

            # Lock held by another request: wait for result
            log.info("idempotency.lock.waiting", key=idempotency_key[:12])
            for attempt in range(lock_ttl * 10):  # Poll up to lock_ttl seconds
                time.sleep(0.1)
                cached = self.client.get(f"idempotent:{idempotency_key}")
                if cached:
                    log.info("idempotency.lock.result_ready", key=idempotency_key[:12], attempt=attempt)
                    return (False, json.loads(cached))

            # Timeout: proceed anyway (degrade gracefully)
            log.warning("idempotency.lock.timeout", key=idempotency_key[:12], ttl=lock_ttl)
            return (True, None)

        except Exception as e:
            log.warning("idempotency.lock.acquisition_failed", key=idempotency_key[:12], error=str(e))
            return (True, None)  # Fallback: proceed without lock

    def release_lock(self, idempotency_key: str) -> bool:
        """Release lock only if we own it (identified by stored lock_value).

        Uses Lua script to ensure atomic check-and-delete (prevents deleting other clients' locks).
        """
        if not self.client or not hasattr(self, '_active_locks'):
            return True

        lock_value = self._active_locks.pop(idempotency_key, None)
        if not lock_value:
            return True  # Not our lock

        lock_key = f"lock:{idempotency_key}"

        # Lua script: only delete if value matches (atomic)
        script = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

        try:
            result = self.client.eval(script, 1, lock_key, lock_value)
            if result == 1:
                log.info("idempotency.lock.released", key=idempotency_key[:12])
                return True
            else:
                log.warning("idempotency.lock.release_failed_not_owner", key=idempotency_key[:12])
                return False
        except Exception as e:
            log.warning("idempotency.lock.release_error", key=idempotency_key[:12], error=str(e))
            return False

    def is_duplicate(self, idempotency_key: str) -> bool:
        """Check if this idempotency key has been seen before."""
        return self.get_cached_response(idempotency_key) is not None
