"""
EntityTypeCache: Runtime cache of valid entity types from database.
Provides database-driven validation without hardcoded VALID_ENTITY_TYPES.
"""

import time
from typing import Set
import logging

log = logging.getLogger(__name__)


class EntityTypeCache:
    """
    Runtime cache of valid entity types from database.
    Refreshes every 5 minutes or when cache is stale.
    """

    def __init__(self, dsn: str, ttl_seconds: int = 300):
        self.dsn = dsn
        self.ttl = ttl_seconds
        self._cache: Set[str] = set()
        self._loaded_at: float = 0.0

    def get_valid_types(self) -> Set[str]:
        """Get current set of valid entity types."""
        now = time.time()
        if now - self._loaded_at > self.ttl or not self._cache:
            self._refresh()
        return self._cache

    def is_valid(self, entity_type: str) -> bool:
        """Check if entity_type is valid (case-insensitive)."""
        return entity_type.lower() in self.get_valid_types()

    def _refresh(self) -> None:
        """Reload entity types from database."""
        try:
            # read_only_connection (src/api/db_read.py): autocommit + readonly + guaranteed
            # close(). `with psycopg2.connect(...)` COMMITS but does NOT close — it abandoned a
            # live backend on every TTL refresh, surviving only until GC (not a guarantee: a
            # traceback or reference cycle pins it indefinitely — one such backend was measured
            # 6 days old). Same defect class as the 2026-08-01 prod idle-in-transaction stall.
            from src.api.db_read import read_only_connection
            with read_only_connection(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT LOWER(entity_type) FROM entity_types WHERE is_learnable=true"
                    )
                    self._cache = {row[0] for row in cur.fetchall()}
                    self._loaded_at = time.time()
                    log.debug(f"EntityTypeCache refreshed: {len(self._cache)} types")
        except Exception as e:
            log.warning(f"EntityTypeCache refresh failed: {e}")
            # Keep old cache on failure
            if not self._cache:
                # Fallback to seed types if DB unavailable
                self._cache = {
                    'person', 'organization', 'location', 'object', 'event', 'animal'
                }


# Global cache instance (initialized at startup)
_ENTITY_TYPE_CACHE = None


def initialize_entity_type_cache(dsn: str):
    """Initialize global entity type cache."""
    global _ENTITY_TYPE_CACHE
    _ENTITY_TYPE_CACHE = EntityTypeCache(dsn)
    _ENTITY_TYPE_CACHE.get_valid_types()  # Warm up cache
    log.info("EntityTypeCache initialized")


def get_entity_type_cache() -> EntityTypeCache:
    """Get global entity type cache."""
    global _ENTITY_TYPE_CACHE
    if _ENTITY_TYPE_CACHE is None:
        raise RuntimeError(
            "EntityTypeCache not initialized. "
            "Call initialize_entity_type_cache() at startup."
        )
    return _ENTITY_TYPE_CACHE
